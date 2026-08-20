from __future__ import annotations

ALLOWED_KINDS = [
    "none",
    "module_create", "module_archive", "module_restore", "module_move",
    "field_add", "field_archive", "field_restore", "field_move", "field_set_property",
    "action_add_or_move", "action_archive", "action_restore", "action_move",
]

PLAN_TOOL = {
    "type": "function",
    "name": "propose_logos_plan",
    "description": (
        "Kullanıcının LOGOS TECH arayüzü için istediği değişikliği güvenli, semantik bir değişiklik planına çevirir. "
        "Bu araç kod, SQL, dosya yolu veya serbest komut üretmez."
    ),
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Kullanıcıya gösterilecek kısa plan özeti."},
            "assistant_text": {"type": "string", "description": "Kısa Türkçe değerlendirme; uygun değilse nedenini ve güvenli alternatifi belirt."},
            "operations": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ALLOWED_KINDS},
                        "module": {"type": "string", "description": "Hedef bölüm adı; gerekmezse boş string."},
                        "target": {"type": "string", "description": "Hedef alan/eylem/bölüm; gerekmezse boş string."},
                        "label": {"type": "string", "description": "Yeni etiket veya değiştirilecek özellik adı; gerekmezse boş string."},
                        "data_type": {"type": "string", "description": "text,longtext,number,money,date,boolean,select,relation; gerekmezse boş string."},
                        "placement": {"type": "string", "description": "page_top,row,form_header,form_footer; gerekmezse boş string."},
                        "direction": {"type": "string", "description": "up veya down; gerekmezse boş string."},
                        "value": {"type": ["string", "number", "boolean", "null"]},
                        "reason": {"type": "string", "description": "İşlemin kısa gerekçesi."},
                    },
                    "required": ["kind", "module", "target", "label", "data_type", "placement", "direction", "value", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["message", "assistant_text", "operations"],
        "additionalProperties": False,
    },
}

SYSTEM_INSTRUCTIONS = """
Sen LOGOS TECH Vision 5.0'ın planlayıcı katmanısın. Kullanıcı Türkçe doğal dil ve bazen ekran görüntüsü verir.
Amaç, isteği anlamak ve SADECE propose_logos_plan aracıyla güvenli bir plan üretmektir.

ZORUNLU KURALLAR:
1. Asla Python, JavaScript, SQL, shell, dosya yolu, patch veya ham kaynak kod önerme/üretme.
2. Asla doğrudan silme veya kalıcı silme planlama. Kullanıcı "sil" derse varsayılan güvenli anlam arşivlemedir.
3. Görselde "buraya", "şuraya", "yanına" gibi ifadeler varsa görsel ve verilen ekran bağlamından semantik hedefi çıkar.
4. Hedef belirsizse operations boş olsun ve assistant_text içinde tek, kısa açıklama/öneri ver. Tahmin ederek riskli değişiklik yapma.
5. Kullanıcı yalnızca "uygun mu?", "nasıl olur?", "incele" diyorsa değişiklik istemediği sürece operations boş olsun; tasarım değerlendirmesi yap.
6. Kurumlar/Finans/Prim korunan çekirdekteyse bu sürümde yapısal işlem planlama; güvenli şekilde bunun 5.x dinamik çekirdek geçişi istediğini söyle.
7. Standart butonlar: Yeni Kayıt, Excel'e İndir, Düzenle, Kopyala, Sil(=arşivle), Kaydet, Vazgeç.
8. Yerleşimler: page_top=sayfa üstü, row=kayıt satırı, form_header=form üstü, form_footer=form altı.
9. Alan tipleri: text, longtext, number, money, date, boolean, select, relation. Formül alanını otomatik oluşturma.
10. Taşıma için direction yalnız up/down. Modül, alan veya eylem sırasını bir adım değiştirir.
11. Bir istek çok büyükse en fazla 8 küçük güvenli işleme böl; geri kalanını ikinci adım olarak öner.
12. Cevaplar kısa, açık ve Türkçe olsun. Kullanıcı teknik terim bilmek zorunda değildir.
""".strip()
