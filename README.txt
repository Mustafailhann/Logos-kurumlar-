LOGOS TECH – DİNAMİK İŞLETME PLATFORMU
VISION 5.0 – Sürüm 5.0.0-alpha.1

AMAÇ
Bu sürüm 4.5 üzerine yeni yamalar eklemek yerine, LOGOS TECH 5.0'ın temiz ve güvenli
mimarisinin ilk çalışan temelidir. 4.5.0-alpha.2 referans sürümü değiştirilmemiştir.
Hedef; kullanıcının kod yazmadan doğal Türkçe + ekran görüntüsü ile programın dinamik
alanlarını yönetmesi ve AI'nin gerçek koda/SQL'e doğrudan erişmemesidir.

TEMEL GÜVENLİK MİMARİSİ
1. OpenAI / Vision yalnızca semantik değişiklik PLANI üretir.
2. AI'ye Python, JavaScript, SQL, shell, dosya yolu veya ham patch yetkisi verilmez.
3. Plan, gerçek DB üzerinde değil SQLite backup API ile üretilen geçici gölge DB üzerinde
   baştan sona denenir.
4. Ön izleme gerçek veriyi değiştirmez.
5. Kullanıcı onayı olmadan plan yayınlanmaz.
6. Yayın öncesi doğrulanmış tam DB yedeği alınır.
7. Çok adımlı uygulama sırasında hata olursa işlem öncesi yedeğe otomatik geri dönüş denenir.
8. Yayın sonrası PRAGMA integrity_check zorunludur.
9. Bütün yazma istekleri merkezi sağlık kontrolü + mutation lock üzerinden seri hale getirilir.
10. DB bozuksa migration/yazma başlamadan fail-closed durur.
11. Beklenmedik kapanma algılanır; sonraki açılışta DB doğrulanır ve kurtarma yedeği alınır.

LOGOS ASİSTAN VISION
- Metin + PNG/JPG/WEBP ekran görüntüsü kabul eder.
- “Buraya Sil ekle”, “şu alanı yukarı taşı”, “bu düzen uygun mu?” gibi doğal Türkçe
  komutlar OpenAI yapılandırıldığında görselle birlikte planlanabilir.
- Görsel yalnız kullanıcı özellikle eklediğinde OpenAI planlayıcısına gönderilir.
- Hedef belirsizse AI'nin tahmin ederek riskli işlem yapması yasaktır; açıklama istemelidir.
- “Sil” varsayılan olarak güvenli Arşivle anlamına gelir; AI kalıcı silme planlayamaz.
- Yeni dinamik bölüm oluşturulduğunda 7 temel güvenli eylem otomatik doğar:
  Yeni Kayıt, Excel'e İndir, Düzenle, Kopyala, Sil/Arşivle, Vazgeç, Kaydet.
- Eylemler güvenli yerleşim alanlarına taşınabilir: sayfa üstü, satır, form üstü, form altı.

OPENAI'YI KOD YAZMADAN BAĞLAMA
1. Programda Ayarlar > LOGOS Asistan Vision > “Asistanı Yapılandır” açın.
2. Model seçin.
3. OpenAI API anahtarınızı girin ve “Ayarları Güvenle Kaydet” deyin.
4. Anahtar API üzerinden tarayıcıya geri gönderilmez.
5. Windows'ta anahtar DPAPI ile mevcut Windows kullanıcısına bağlı olarak korunur.
6. OpenAI bağlı değilse yerel güvenli komut asistanı çalışmaya devam eder; görsel analizi yapılmaz.

MODEL SEÇENEKLERİ
- GPT-5.6 Sol (gpt-5.6): en güçlü seçenek.
- GPT-5.6 Terra: dengeli seçenek.
- GPT-5.6 Luna: ekonomik seçenek.

ÇÖKME / VERİ KORUMA
“Hiçbir yazılım asla çökmez” şeklinde gerçekçi olmayan bir garanti verilmez. 5.0'ın yaklaşımı:
çökme veya hata olsa bile mümkün olduğunca veri yazmamak/bozmamak, yarım değişikliği geri
almak, bütünlük doğrulaması yapmak ve doğrulanmış yedek bırakmaktır.

GÜVENLİ ALPHA TESTİ
Baslat.bat gerçek veri klasörünü doğrudan açmaz. Ayrı test klasörü kullanır:
%LOCALAPPDATA%\Sekizdesekiz\OkulGuvenligi_5_0_VISION_ALPHA1_TEST
İlk açılışta gerçek DB varsa SQLite backup API ile tek seferlik test kopyası alınır.
Gerçek veritabanı bu alpha paketinin yazma hedefi değildir.

BAŞLATMA
1. ZIP'i normal bir klasöre çıkarın.
2. Baslat.bat'a çift tıklayın.
3. İlk testte OpenAI anahtarı girmek zorunda değilsiniz; yerel güvenli mod çalışır.
4. Vision denemek için Ayarlar > LOGOS Asistan Vision'dan API bağlantısını yapılandırın.

SİSTEM GEREKSİNİMİ
- Windows 10 / 11
- Python 3.11+
- Temel program çevrimdışı çalışır.
- OpenAI Vision özellikleri için internet + kullanıcı tarafından sağlanan OpenAI API anahtarı gerekir.

ÖNEMLİ SINIR – ALPHA.1
Bu sürüm %98 hedefinin tamamlandığı anlamına gelmez. Kurum / Finans / Prim hâlâ tamamen
ortak dinamik çekirdeğe taşınmış değildir. 5.0.0-alpha.1'in görevi önce güvenlik, Vision,
planlama, gölge test ve kurtarma omurgasını kurmaktır. Sonraki sürümlerde çekirdek modül
göçü, layout/component motoru, yetki ve otomasyon motorları bu omurgaya bağlanacaktır.
