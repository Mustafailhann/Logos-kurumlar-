from __future__ import annotations

import re
import json
import time
import secrets
import hashlib
import threading
import urllib.request
import urllib.parse
import http.cookiejar
from typing import Any
from pathlib import Path


def parse_portal_html_or_text(raw_input: str) -> list[dict[str, Any]]:
    if not raw_input or not raw_input.strip():
        return []

    # 0. Check for direct JSON payload or embedded DataStore script JSON objects
    json_candidates = []
    if "adi" in raw_input or "kurum_kodu" in raw_input or "hatalar" in raw_input:
        try:
            parsed = json.loads(raw_input)
            if isinstance(parsed, list):
                json_candidates = parsed
            elif isinstance(parsed, dict) and "liste" in parsed:
                json_candidates = parsed["liste"]
            elif isinstance(parsed, dict) and "institutions" in parsed:
                json_candidates = parsed["institutions"]
            elif isinstance(parsed, dict) and "data" in parsed:
                json_candidates = parsed["data"]

        except Exception:
            pass

        if not json_candidates:
            # Extract JSON arrays from <script> tags or JS variables: e.g. [{ "id": ..., "adi": ..., "hatalar": [...] }]
            script_json_matches = re.findall(r'\[\s*\{\s*"id"\s*:.*?\}\s*\]', raw_input, re.DOTALL)
            for s_json in script_json_matches:
                try:
                    p = json.loads(s_json)
                    if isinstance(p, list) and len(p) > 0 and ("adi" in p[0] or "name" in p[0]):
                        json_candidates = p
                        break
                except Exception:
                    pass

    if json_candidates:
        records = []
        for item in json_candidates:
            if not isinstance(item, dict):
                continue
            name = str(item.get("adi") or item.get("name") or "").strip()
            if not name:
                continue
            name_upper = name.upper()

            # Rule 1: Exclude Anaokulu / Kreş / Yuva / Okul Öncesi
            if any(kw in name_upper for kw in ["ANAOKUL", "ANASINIF", "KREŞ", "KRES", "YUVA", "MONTESSORI", "KINDERGARTEN", "OKUL ÖNCESİ", "OKUL ONCESI"]):
                continue

            # Rule 2: Dealer must be LOGOS (by_val can be '260', '6737', or 'LOGOS')
            by_val = str(item.get("by") or item.get("bayi_id") or item.get("dealer") or "").strip()
            is_logos = (not by_val or by_val in ["260", "6737", "LOGOS"] or "LOGOS" in by_val.upper())
            if not is_logos:
                continue
            dealer = "LOGOS"

            # Rule 3: Product must be Kapı Kontrol
            urun_info = item.get("urun_bilgileri") or {}
            kapi_info = urun_info.get("kapi_kontrol") if isinstance(urun_info, dict) else {}
            if isinstance(kapi_info, dict) and kapi_info.get("kullaniyor") is False:
                continue

            # Rule 4: Exclude Rakibe Gidenler
            alt_products = []
            if isinstance(kapi_info, dict):
                for k, v in kapi_info.items():
                    if isinstance(v, dict):
                        alt_products.append(v)
                if not alt_products:
                    alt_products.append(kapi_info)

            rakibe_gitti = False
            for alt in alt_products:
                if alt.get("rakibe_gitti") is True or str(alt.get("rakibe_gitti")) in ["1", "true"]:
                    rakibe_gitti = True
                    break
            if rakibe_gitti:
                continue


            # formatHatalar logic matching portal's error messages
            hatalar_raw = item.get("hatalar") or item.get("notes") or []
            notes_parts = []
            if isinstance(hatalar_raw, list):
                for h in hatalar_raw:
                    if isinstance(h, str) and h.strip():
                        notes_parts.append(h.strip())
                    elif isinstance(h, dict):
                        txt = h.get("mesaj") or h.get("baslik") or h.get("aciklama") or h.get("tanim") or h.get("hata") or h.get("metin") or h.get("title") or h.get("name")
                        if txt:
                            notes_parts.append(str(txt).strip())
                        else:
                            parts = [str(v) for v in h.values() if isinstance(v, (str, int, float))]
                            if parts:
                                notes_parts.append(" - ".join(parts))
            elif isinstance(hatalar_raw, str) and hatalar_raw.strip():
                notes_parts.append(hatalar_raw.strip())

            notes = " · ".join(notes_parts)
            health_status = "Kurumda Hatalar Var" if notes else "Kurumda sorun yok"
            customer_status = "AKTİF" if (item.get("aktif") in [1, "1", True]) else "PASİF"

            # Parse panels / terminaller
            panels = []
            terminaller = item.get("terminaller") or []
            if isinstance(terminaller, list):
                for term in terminaller:
                    if not isinstance(term, dict):
                        continue
                    t_name = str(term.get("terminal_takma_adi") or term.get("terminal_ismi") or "Turnike Paneli").strip()
                    local_ip = str(term.get("lokal_ip") or "").strip()
                    alpemix = str(term.get("alpemix_adi") or "").strip()
                    last_seen = str(term.get("son_aktiflik_zamani") or "").strip()

                    versiyon = term.get("versiyon_bilgisi") or {}
                    v_str = f"{versiyon.get('program', '')} / {versiyon.get('versiyon_guncelleme', '')}".strip(" /")

                    modem = term.get("modem_bilgileri") or {}
                    modem_name = str(modem.get("modem") or "").strip()
                    operator = str(modem.get("gsm_operator") or "").strip()
                    phone = str(modem.get("gsm") or "").strip()

                    cameras = term.get("hareket_kamerasi") or []
                    e_cam_status, x_cam_status = "", ""
                    e_cam_ip, x_cam_ip = "", ""
                    if isinstance(cameras, list):
                        for cam in cameras:
                            if not isinstance(cam, dict):
                                continue
                            cam_name = str(cam.get("kamera_adi") or "").upper()
                            direction = cam.get("hareket_yonu")
                            durum = cam.get("durum") is True

                            if direction == 1 or "GİRİŞ" in cam_name:
                                e_cam_status = "GİRİŞ KAMERASI AKTİF" if durum else "GİRİŞ KAMERASI PASİF"
                                e_cam_ip = str(cam.get("ip") or "").strip()
                            elif direction == 0 or "ÇIKIŞ" in cam_name:
                                x_cam_status = "ÇIKIŞ KAMERASI AKTİF" if durum else "ÇIKIŞ KAMERASI PASİF"
                                x_cam_ip = str(cam.get("ip") or "").strip()

                    panels.append({
                        "panel_key": alpemix or f"panel_{len(panels)+1}",
                        "name": t_name,
                        "local_ip": local_ip,
                        "software_version": v_str,
                        "modem": modem_name,
                        "operator": operator,
                        "phone": phone,
                        "last_connection": last_seen,
                        "entry_camera_status": e_cam_status,
                        "entry_camera_ip": e_cam_ip,
                        "exit_camera_status": x_cam_status,
                        "exit_camera_ip": x_cam_ip,
                    })

            records.append({
                "portal_id": str(item.get("id") or item.get("portal_id") or "").strip(),
                "institution_code": str(item.get("kurum_kodu") or item.get("institution_code") or "").strip(),
                "name": name,
                "city": str(item.get("il") or item.get("city") or "").strip(),
                "district": str(item.get("ilce") or item.get("district") or "").strip(),
                "health_status": health_status,
                "customer_status": customer_status,
                "notes": notes,
                "sales_period": str(item.get("sd") or item.get("sales_period") or ""),
                "sales_person": str(item.get("st") or item.get("sales_person") or ""),
                "dealer": dealer,
                "marketing_person": str(item.get("satis_temsilcisi") or item.get("marketing_person") or ""),
                "customer_person": str(item.get("musteri_temsilcisi") or item.get("customer_person") or ""),
                "technical_person": str(item.get("teknik_servis_temsilcisi") or item.get("technical_person") or ""),
                "accounting_person": str(item.get("muhasebe_temsilcisi") or item.get("accounting_person") or ""),
                "panels": panels,
            })
        if records:
            return records


    # Clean HTML tags if present, preserving breaks
    clean = re.sub(r'<style[^>]*>.*?</style>', '', raw_input, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<script[^>]*>.*?</script>', '', clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<br\s*/?>', '\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'</?(div|p|tr|td|li|section|header|h1|h2|h3)[^>]*>', '\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'<[^>]+>', ' ', clean)

    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    full_str = "\n".join(lines)

    # Institution block matcher
    inst_pattern = re.compile(
        r'([A-ZÇĞİÖŞÜ\s]+)\s*-\s*([A-ZÇĞİÖŞÜ\s]+)\s*-\s*([^\n]+)\s*\n+\s*ID:\s*(\d+)\s+KURUM\s+KODU:\s*(\d+)',
        re.IGNORECASE
    )

    matches = list(inst_pattern.finditer(full_str))
    records = []


    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(full_str)
        block = full_str[start:end]

        city = match.group(1).strip()
        district = match.group(2).strip()
        name = match.group(3).strip()
        portal_id = match.group(4).strip()
        institution_code = match.group(5).strip()

        # Rule 1: Exclude Anaokulu / Kreş / Okul Öncesi
        name_upper = name.upper()
        if any(kw in name_upper for kw in ["ANAOKULU", "ANAOKULLARI", "ANA OKULU", "ANA OKULLARI", "KREŞ", "KRES", "OKUL ÖNCESİ"]):
            continue

        # Parse Representatives
        by_match = re.search(r'BY:\s*([^\n]+)', block)
        mt_match = re.search(r'MT:\s*([^\n]+)', block)
        ts_match = re.search(r'TS:\s*([^\n]+)', block)
        mh_match = re.search(r'MH:\s*([^\n]+)', block)
        st_match = re.search(r'ST:\s*([^\n]+)', block)
        sd_match = re.search(r'SD:\s*([^\n]+)', block)

        dealer = by_match.group(1).strip() if by_match else "LOGOS"

        # Rule 2: Exclude non-LOGOS dealers (Only fetch LOGOS dealer institutions)
        if dealer and "LOGOS" not in dealer.upper():
            continue

        customer_person = mt_match.group(1).strip() if mt_match else ""
        technical_person = ts_match.group(1).strip() if ts_match else ""
        accounting_person = mh_match.group(1).strip() if mh_match else ""
        sales_person = st_match.group(1).strip() if st_match else ""
        sales_period = sd_match.group(1).strip() if sd_match else ""

        # Rating
        rating_match = re.search(r'([0-5])\s*Puan', block, re.IGNORECASE)
        rating = int(rating_match.group(1)) if rating_match else 5

        # Statuses & Badges Parsing
        block_upper = block.upper()
        rental_status = "KİRALIK" if "KİRALIK" in block_upper else ("SATILIK" if "SATILIK" in block_upper else "")
        payment_status = "ÖDEMESİNİ YAPTI" if "ÖDEMESİNİ YAPTI" in block_upper else ""

        if "PAZARLAMA AŞAMASINDA İPTAL" in block_upper:
            customer_status = "PAZARLAMA AŞAMASINDA İPTAL"
        elif "GEÇİCİ KULLANIM DIŞI" in block_upper:
            customer_status = "GEÇİCİ KULLANIM DIŞI"
        elif "PASİF" in block_upper:
            customer_status = "PASİF"
        elif "AKTİF" in block_upper:
            customer_status = "AKTİF"
        else:
            customer_status = "AKTİF"

        # Red Error Box & Portal Banner Parsing (Automatic Background Sync)
        notes_list = []
        if "PAZARLAMA AŞAMASINDA İPTAL" in block_upper:
            notes_list.append("PAZARLAMA AŞAMASINDA İPTAL")
        if "MUHASEBE İŞLEMLERİ BAŞLAMADI" in block_upper:
            notes_list.append("MUHASEBE İŞLEMLERİ BAŞLAMADI")

        # 1. Catch ALL lines following "Görev Aç" buttons (portal error section)
        gorev_ac_matches = re.findall(r'Görev\s+Aç\s*([^\n<]+)', block, re.IGNORECASE)
        for g_err in gorev_ac_matches:
            cleaned_err = g_err.strip()
            if cleaned_err and cleaned_err not in notes_list:
                notes_list.append(cleaned_err)

        # 2. Catch red camera badges like "ÇIKIŞ KAMERASI PASİF", "GİRİŞ KAMERASI PASİF"
        cam_pasif_matches = re.findall(r'(?:GİRİŞ|ÇIKIŞ)?\s*KAMERAS[Iİ]\s*PASİF', block, re.IGNORECASE)
        for cp_err in cam_pasif_matches:
            c_str = cp_err.strip()
            if c_str and c_str not in notes_list:
                notes_list.append(c_str)

        # 3. Catch explicit error boxes / banners
        error_box_matches = re.findall(r'Kurumda\s+[^\n<]+', block, re.IGNORECASE)
        for err_txt in error_box_matches:
            cleaned_err = err_txt.strip()
            if cleaned_err and cleaned_err not in notes_list:
                notes_list.append(cleaned_err)

        # 4. Catch Kapı Kontrol Kapalı or Arızalı
        kapali_matches = re.findall(r'Kapı\s+Kontrol\s*\d*\s*(?:Kapalı|Arızalı|Hatalı)', block, re.IGNORECASE)
        for k_err in kapali_matches:
            if k_err.strip() and k_err.strip() not in notes_list:
                notes_list.append(k_err.strip())

        # 5. Catch GSM / Operator / Reader / Attendance / Camera errors
        extra_err_matches = re.findall(r'[^\n<]*?\b(?:arızalı|pasif|hatalı|alınamadı|kesildi|yoklamada|yoklamaya)\b[^\n<]*', block, re.IGNORECASE)
        for ex_err in extra_err_matches:
            cleaned = ex_err.strip()
            if cleaned and len(cleaned) > 5 and cleaned not in notes_list:
                # Filter out active status words
                if not any(good in cleaned.upper() for good in ["KÖK NEDEN", "GEMİNİ", "SORUN YOK", "AKTİF"]):
                    notes_list.append(cleaned)

        notes = " · ".join(notes_list)



        # Health status evaluation
        has_error = False
        if any(err_word in block_upper for err_word in ["HATA", "PASİF", "ÇALIŞMIYOR", "KAPALI", "OFFLINE", "KESİNTİ", "SORUN", "ARIZA", "BORÇLU", "ÖDEMEDİ", "İPTAL"]):
            has_error = True
        if rating <= 2 or notes_list:
            has_error = True

        health_status = "Kurumda Hatalar Var" if has_error else "Kurumda sorun yok"


        # Parse panels inside block
        panels = []
        panel_match = re.search(r'(TEK TURNİKELİ|\d+\s*TURNİKELİ|TURNİKESİZ)\s*([^\n]*)', block, re.IGNORECASE)
        if panel_match:
            turnstile_text = panel_match.group(1).upper()
            if "TEK" in turnstile_text:
                turnstile_count = 1
            elif "TURNİKESİZ" in turnstile_text:
                turnstile_count = 0
            else:
                num_match = re.search(r'\d+', turnstile_text)
                turnstile_count = int(num_match.group(0)) if num_match else 1

            gate_raw = panel_match.group(2).strip()
            gate_name = re.sub(r'Kapı Kontrol.*', '', gate_raw, flags=re.IGNORECASE).strip() or "ANA KAPI"

            # Local IP
            all_ips = re.findall(r'(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)', block)
            local_ip = all_ips[0] if all_ips else ""

            # Panel Key (e.g. Ktbogazicivip, Ktnizipfinal)
            panel_key_match = re.search(r'\b(Kt[a-zA-Z0-9_-]+)\b', block, re.IGNORECASE)
            panel_key = panel_key_match.group(1) if panel_key_match else f"panel_{portal_id}"

            # Software Version
            ver_match = re.search(r'\b(\d+\.\d+\.\d+\.\d+)\b', block)
            version = ver_match.group(1) if ver_match else ""

            # Camera IPs & RTSP
            camera_ips = re.findall(r'IP:\s*((?:\d{1,3}\.){3}\d{1,3})', block)
            camera_rtsps = re.findall(r'RTSP:\s*(\d+)', block)

            entry_ip = camera_ips[0] if len(camera_ips) > 0 else ""
            exit_ip = camera_ips[1] if len(camera_ips) > 1 else ""
            entry_rtsp = camera_rtsps[0] if len(camera_rtsps) > 0 else ""
            exit_rtsp = camera_rtsps[1] if len(camera_rtsps) > 1 else ""

            # Modem & Operator
            modem = "Keenetic" if "KEENETIC" in block.upper() else ""
            operator = "Vodafone" if "VODAFONE" in block.upper() else ("Avea" if "AVEA" in block.upper() else ("Turkcell" if "TURKCELL" in block.upper() else ""))
            phone_match = re.search(r'(?<!\d)0?5\d{9}(?!\d)', block.replace(" ", ""))
            phone = phone_match.group(0) if phone_match else ""

            panels.append({
                "panel_key": panel_key,
                "name": "Kapı Kontrol 1",
                "gate_name": gate_name,
                "product_name": "Kapı Kontrol",
                "turnstile_count": turnstile_count,
                "local_ip": local_ip,
                "software_version": version,
                "modem": modem,
                "operator": operator,
                "phone": phone,
                "entry_camera_status": "AKTİF" if "GİRİŞ KAMERASI AKTİF" in block.upper() else "",
                "entry_camera_ip": entry_ip,
                "entry_camera_rtsp": entry_rtsp,
                "exit_camera_status": "AKTİF" if "ÇIKIŞ KAMERASI AKTİF" in block.upper() else "",
                "exit_camera_ip": exit_ip,
                "exit_camera_rtsp": exit_rtsp,
                "status": "Hatalı" if has_error else "Kayıtlı"
            })

        records.append({
            "portal_id": portal_id,
            "institution_code": institution_code,
            "name": name,
            "city": city,
            "district": district,
            "school_type": "Okul",
            "dealer": dealer,
            "customer_person": customer_person,
            "technical_person": technical_person,
            "accounting_person": accounting_person,
            "sales_person": sales_person,
            "sales_period": sales_period,
            "rental_status": rental_status,
            "customer_status": customer_status,
            "payment_status": payment_status,
            "notes": notes,
            "active": 1,
            "source": "portal_web_sync",
            "panels": panels
        })

    return records


def fetch_portal_with_cookie(cookie_string: str, url: str = "https://okulguvenligi.com/uye/panel/kurumlar") -> list[dict[str, Any]]:
    if not cookie_string or not cookie_string.strip():
        raise ValueError("Oturum çerezi (Cookie) bulunamadı.")

    t_now = time.strftime('%H:%M:%S')
    print(f"[PORTAL SYNC {t_now}] 🌐 okulguvenligi.com canlı veri servisi çağrılıyor (islem=liste)...")

    # 1. Primary Method: POST islem=liste (fetches 596 institutions + errors as 4MB JSON directly from portal AJAX endpoint)
    post_data = urllib.parse.urlencode({"islem": "liste"}).encode("utf-8")
    req_post = urllib.request.Request(url, data=post_data, method="POST")
    req_post.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0")
    req_post.add_header("Cookie", cookie_string.strip())
    req_post.add_header("X-Requested-With", "XMLHttpRequest")

    try:
        with urllib.request.urlopen(req_post, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            records = parse_portal_html_or_text(body)
            t_done = time.strftime('%H:%M:%S')
            print(f"[PORTAL SYNC {t_done}] ✅ okuguvenligi.com yanıt verdi: {len(records)} aktif filtreli kurum & arızalar alındı.")
            if records and len(records) > 0:
                return records
    except Exception as exc:
        print(f"[PORTAL SYNC {t_now}] ⚠️ POST islem=liste uyarısı: {exc}")

    # 2. Secondary Fallback: Standard GET request
    req_get = urllib.request.Request(url, method="GET")
    req_get.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0")
    req_get.add_header("Cookie", cookie_string.strip())

    try:
        with urllib.request.urlopen(req_get, timeout=12) as resp:
            html_content = resp.read().decode("utf-8", errors="ignore")
            records = parse_portal_html_or_text(html_content)
            if not records and ("giris" in resp.url.lower() or "login" in resp.url.lower()):
                raise ValueError("Oturum süresi dolmuş. Lütfen uygulamadan tekrar SMS onayı ile giriş yapın.")
            return records
    except Exception as exc:
        raise ValueError(f"okulguvenligi.com bağlantı hatası: {exc}")


def extract_cookies_from_response_and_jar(resp: Any, cj: Any) -> str:
    cookie_dict: dict[str, str] = {}
    if cj:
        for c in cj:
            if c.name and c.value and c.value.lower() != "deleted":
                cookie_dict[c.name] = c.value
    if resp and hasattr(resp, "headers"):
        set_cookies = resp.headers.get_all("Set-Cookie") or []
        for header in set_cookies:
            parts = header.split(";")
            if parts and "=" in parts[0]:
                kv = parts[0].strip().split("=", 1)
                k, v = kv[0].strip(), kv[1].strip()
                if v and v.lower() != "deleted":
                    cookie_dict[k] = v
    cookie_dict.setdefault("site_kullanim_modu", "kapi")
    return "; ".join(f"{k}={v}" for k, v in cookie_dict.items())


class PortalLoginSessionStore:
    """Manages 2-Step Interactive Portal Login with SMS OTP & Automatic PHPSESSID Extraction"""
    def __init__(self):
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start_login(self, username: str, password: str) -> dict[str, Any]:
        t_now = time.strftime('%H:%M:%S')
        print(f"\n[PORTAL LOGIN {t_now}] 🔑 1/3 Giriş isteği başlatılıyor... Kullanıcı: {username}")
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        opener.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0")]

        url = "https://okulguvenligi.com/uye/panel/kurumlar"
        post_data = urllib.parse.urlencode({
            "islem": "giris-yap",
            "kurum_kodu": username,
            "sifre": password
        }).encode("utf-8")

        try:
            req = urllib.request.Request(url, data=post_data, method="POST")
            resp = opener.open(req, timeout=12)
            resp_bytes = resp.read()

            try:
                resp_json = json.loads(resp_bytes.decode("utf-8", errors="ignore"))
                if resp_json.get("basarili") is False:
                    raise ValueError(resp_json.get("hata", "Giriş bilgileri hatalı."))
            except json.JSONDecodeError:
                pass

            session_id = secrets.token_urlsafe(16)
            with self._lock:
                self._sessions[session_id] = {
                    "cj": cj,
                    "opener": opener,
                    "username": username,
                    "password": password,
                    "created": time.time()
                }

            t_done = time.strftime('%H:%M:%S')
            print(f"[PORTAL LOGIN {t_done}] 📲 1/3 SMS Kodu Gönderildi! Telefonunuzu kontrol ediniz.")

            return {
                "session_id": session_id,
                "sms_required": True,
                "message": "📲 SMS doğrulama kodu telefonunuza gönderildi. Lütfen gelen kodu giriniz."
            }
        except Exception as exc:
            print(f"[PORTAL LOGIN {t_now}] ❌ Giriş hatası: {exc}")
            raise ValueError(f"okulguvenligi.com giriş hatası: {exc}")

    def verify_sms(self, session_id: str, sms_code: str) -> dict[str, Any]:
        t_now = time.strftime('%H:%M:%S')
        print(f"\n[PORTAL LOGIN {t_now}] 📩 2/3 SMS Kodu Doğrulanıyor: {sms_code}")
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise ValueError("Oturum süresi doldu. Lütfen tekrar giriş yapın.")

        opener = session["opener"]
        cj = session["cj"]
        username = session["username"]
        password = session["password"]

        url = "https://okulguvenligi.com/uye/panel/kurumlar"
        post_data = urllib.parse.urlencode({
            "islem": "sms-onay",
            "kurum_kodu": username,
            "sifre": password,
            "onay_kodu": sms_code
        }).encode("utf-8")

        try:
            req = urllib.request.Request(url, data=post_data, method="POST")
            resp = opener.open(req, timeout=12)
            post_str = resp.read().decode("utf-8", errors="ignore")

            # Extract full cookie string from response & jar
            cookie_str = extract_cookies_from_response_and_jar(resp, cj)

            # Check JSON response from sms-onay POST
            try:
                res_json = json.loads(post_str)
                if isinstance(res_json, dict) and res_json.get("basarili") is False:
                    err = res_json.get("hata") or "SMS doğrulama kodu hatalı. Lütfen tekrar deneyin."
                    raise ValueError(err)
            except json.JSONDecodeError:
                pass

            t_auth = time.strftime('%H:%M:%S')
            print(f"[PORTAL LOGIN {t_auth}] ✅ 2/3 SMS Doğrulandı! Canlı okullar ve arızalar çekiliyor...")

            records = []
            try:
                records = fetch_portal_with_cookie(cookie_str, "https://okulguvenligi.com/uye/panel/kurumlar")
            except Exception as exc:
                print(f"[PORTAL LOGIN {t_auth}] ⚠️ Veri çekim uyarısı: {exc}")

            if not records:
                try:
                    records = fetch_portal_with_cookie(cookie_str, "https://okulguvenligi.com/uye/panel/")
                except Exception:
                    pass

            t_ok = time.strftime('%H:%M:%S')
            print(f"[PORTAL LOGIN {t_ok}] 🎉 3/3 Başarılı! Toplam {len(records)} canlı kurum veritabanına aktarıldı.")

            return {
                "cookie_string": cookie_str,
                "username": username,
                "password": password,
                "records": records,
                "message": "✅ SMS doğrulaması başarılı! Portaldan canlı veriler güncellendi ve 30 dk otomatik çekim görevi aktif edildi."
            }

        except Exception as exc:
            raise ValueError(f"SMS doğrulama hatası: {exc}")


def fetch_portal_live(username: str, password: str, cookie_string: str = "", url: str = "https://okulguvenligi.com/uye/panel/kurumlar") -> list[dict[str, Any]]:
    if cookie_string and cookie_string.strip():
        return fetch_portal_with_cookie(cookie_string, url)

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0")]

    post_data = urllib.parse.urlencode({
        "islem": "giris-yap",
        "kurum_kodu": username,
        "sifre": password
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=post_data, method="POST")
        opener.open(req, timeout=10)
    except Exception:
        pass

    try:
        req_page = urllib.request.Request(url, method="GET")
        resp_page = opener.open(req_page, timeout=15)
        html_content = resp_page.read().decode("utf-8", errors="ignore")
        return parse_portal_html_or_text(html_content)
    except Exception as exc:
        raise ValueError(f"okulguvenligi.com bağlantı hatası: {exc}")


class PortalAutoSyncManager:
    def __init__(self, db: Any):
        self.db = db
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.enabled = False
        self.mode = "cookie"
        self.cookie_string = ""
        self.username = ""
        self.password = ""
        self.interval_minutes = 30
        self.last_sync_at = ""
        self.next_sync_at = ""
        self.last_status = "idle"
        self.last_error = ""
        self.sync_count = 0
        self._load_config()

    def _load_config(self):
        config_raw = self.db.get_setting("portal_auto_sync_config", "{}")
        try:
            cfg = json.loads(config_raw)
            self.enabled = bool(cfg.get("enabled", False))
            self.mode = str(cfg.get("mode", "cookie"))
            self.cookie_string = str(cfg.get("cookie_string", ""))
            self.username = str(cfg.get("username", ""))
            self.password = str(cfg.get("password", ""))
            self.interval_minutes = int(cfg.get("interval_minutes", 30))
            self.last_sync_at = str(cfg.get("last_sync_at", ""))
            self.last_status = str(cfg.get("last_status", "idle"))
        except Exception:
            pass

    def save_config(self, enabled: bool, mode: str = "cookie", cookie_string: str = "", username: str = "", password: str = "", interval_minutes: int = 30, trigger_now: bool = False) -> dict[str, Any]:
        with self._lock:
            self.enabled = enabled
            self.mode = mode
            if cookie_string and cookie_string.strip():
                self.cookie_string = cookie_string.strip()
            self.username = username.strip()
            if password and password.strip():
                self.password = password.strip()
            self.interval_minutes = max(1, min(1440, interval_minutes))

            cfg = {
                "enabled": self.enabled,
                "mode": self.mode,
                "cookie_string": self.cookie_string,
                "username": self.username,
                "password": self.password,
                "interval_minutes": self.interval_minutes,
                "last_sync_at": self.last_sync_at,
                "last_status": self.last_status,
            }
            self.db.set_setting("portal_auto_sync_config", json.dumps(cfg, ensure_ascii=False))

        if trigger_now and self.enabled:
            threading.Thread(target=self.trigger_sync, daemon=True).start()

        return self.status()

    def status(self) -> dict[str, Any]:
        last_time = self.last_sync_at
        if last_time and "T" in last_time:
            last_time = last_time.replace("T", " ")
        if last_time and "+" in last_time:
            last_time = last_time.split("+")[0]

        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "has_cookie": bool(self.cookie_string),
            "cookie_sample": (self.cookie_string[:24] + "...") if self.cookie_string else "",
            "username": self.username,
            "has_password": bool(self.password),
            "interval_minutes": self.interval_minutes,
            "last_sync_at": last_time if last_time else None,
            "next_sync_at": self.next_sync_at if self.enabled else None,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "sync_count": self.sync_count,
        }





    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        last_sync_time = 0
        while not self._stop_event.is_set():
            time.sleep(5)
            if not self.enabled:
                continue
            if not self.cookie_string and (not self.username or not self.password):
                continue

            now = time.time()
            interval_seconds = self.interval_minutes * 60
            if last_sync_time == 0 or (now - last_sync_time) >= interval_seconds:
                last_sync_time = now
                self.trigger_sync()

    def trigger_sync(self) -> dict[str, Any]:
        self.last_status = "running"
        try:
            if not self.cookie_string or not self.cookie_string.strip():
                self.last_status = "error"
                self.last_error = "Henüz canlı oturum açılmamış. Lütfen SMS girişi yapın."
                return {"ok": False, "error": self.last_error}

            records = []
            try:
                records = fetch_portal_with_cookie(self.cookie_string)
            except Exception as exc:
                self.last_status = "error"
                self.last_error = str(exc)
                return {"ok": False, "error": str(exc)}

            if records:
                sha256 = hashlib.sha256(f"{self.cookie_string[:10]}_{len(records)}_{time.time()}".encode("utf-8")).hexdigest()
                result = self.db.import_records(records, f"{self.interval_minutes} Dk Otomatik Portal Senkronizasyonu", sha256)
                self.db.recalculate_health_statuses()
                self.sync_count += 1
                self.last_sync_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self.last_status = "ok"
                self.last_error = ""
                next_t = time.time() + (self.interval_minutes * 60)
                self.next_sync_at = time.strftime("%H:%M:%S", time.localtime(next_t))
                return {"ok": True, "data": result}
            else:
                self.last_status = "error"
                self.last_error = "okulguvenligi.com sayfasından geçerli veri okunamadı. Oturum süresi dolmuş olabilir."
                return {"ok": False, "error": self.last_error}
        except Exception as exc:
            self.last_status = "error"
            self.last_error = str(exc)
            return {"ok": False, "error": str(exc)}


