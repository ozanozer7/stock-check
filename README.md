# Amazon.com.tr Stok Takipçisi

Alışveriş listenizdeki stokta olmayan ürünler stoğa girdiğinde e-posta ve/veya Telegram ile haber verir. GitHub Actions üzerinde saatte bir, ücretsiz çalışır; bilgisayarınızın açık olması gerekmez.

## 1. Alışveriş listesini paylaşılabilir yapın

Amazon.com.tr → Listeleriniz → ilgili liste → **Diğer** (⋯) → **Listeyi yönet** → Gizlilik: **Paylaşılan** veya **Herkese açık**. Ardından **Listeyi paylaş** ile bağlantıyı kopyalayın (ör. `https://www.amazon.com.tr/hz/wishlist/ls/XXXXXXXX?...`).

## 2. GitHub deposu oluşturun

1. github.com'da ücretsiz hesap açın, **New repository** ile **Private** bir depo oluşturun.
2. Bu klasördeki tüm dosyaları (gizli `.github` klasörü dahil) depoya yükleyin. Web arayüzünde "Add file → Upload files" ile klasörü sürükleyip bırakabilirsiniz; `.github/workflows/stok-takip.yml` dosyasının yüklendiğinden emin olun.

## 3. Bildirim kanalını ayarlayın (birini veya ikisini)

### Telegram (önerilir, anında telefona düşer)
1. Telegram'da **@BotFather**'a `/newbot` yazın, bota bir ad verin. Size verilen **token**'ı kopyalayın.
2. Yeni botunuza bir mesaj gönderin (ör. "merhaba").
3. Tarayıcıda `https://api.telegram.org/bot<TOKEN>/getUpdates` adresini açın; `"chat":{"id": 123456789` kısmındaki sayı sizin **chat ID**'nizdir.

### E-posta (Gmail)
1. Google hesabınızda 2 adımlı doğrulamayı açın.
2. https://myaccount.google.com/apppasswords adresinden bir **uygulama şifresi** oluşturun (normal Gmail şifreniz çalışmaz).

## 4. Ayarları GitHub'a girin

Depo → **Settings → Secrets and variables → Actions → New repository secret**:

| Ad | Değer |
|---|---|
| `WISHLIST_URLS` | Liste bağlantısı (birden fazlaysa virgülle ayırın) |
| `TELEGRAM_BOT_TOKEN` | BotFather'ın verdiği token |
| `TELEGRAM_CHAT_ID` | Chat ID'niz |
| `SMTP_USER` | Gmail adresiniz |
| `SMTP_PASSWORD` | 16 haneli uygulama şifresi |
| `EMAIL_TO` | Bildirimin gideceği adres (boşsa SMTP_USER'a gider) |
| `PRODUCT_URLS` | (İsteğe bağlı) Listede olmayan ekstra ürün bağlantıları |

Kullanmadığınız kanalın ayarlarını boş bırakmanız yeterli.

İsteğe bağlı: Ürün Amazon'da yokken başka satıcılarda bulunduğunda da haber almak isterseniz **Variables** sekmesinde `COUNT_OTHER_SELLERS` = `true` ekleyin.

## 5. Başlatın

Depo → **Actions** sekmesi → (istenirse workflow'ları etkinleştirin) → **Amazon stok takibi** → **Run workflow**. İlk çalışmada "Stok takibi başladı" bildirimi gelir; bu, ayarların doğru olduğunu gösterir. Bundan sonra her saat otomatik çalışır ve yalnızca bir ürün **stokta yok → stokta** durumuna geçtiğinde haber verir.

Kontrol sıklığını değiştirmek için `.github/workflows/stok-takip.yml` içindeki `cron` satırını düzenleyin (ör. her 2 saatte bir: `"17 */2 * * *"`). Çok sık kontrol, Amazon'un engelleme ihtimalini artırır.

## Bilinmesi gerekenler

- **Engelleme:** Amazon, GitHub sunucularından gelen istekleri zaman zaman CAPTCHA ile engelleyebilir. Script bu durumda o çalışmayı sessizce atlar; art arda 6 kez engellenirse size bir uyarı gönderir. Sorun sürerse scripti kendi bilgisayarınızda çalıştırmak (aşağıda) genellikle daha iyi sonuç verir.
- **Sayfa yapısı:** Amazon sayfa tasarımını değiştirirse scriptin güncellenmesi gerekebilir. Actions sekmesindeki çalışma kayıtlarında her ürünün durumu yazdırılır; bir şey ters giderse oradan görebilirsiniz.
- Depo **Private** olduğu sürece GitHub'ın ücretsiz aylık 2.000 dakikalık Actions kotası saatlik kontrol için fazlasıyla yeter.

## Kendi bilgisayarınızda çalıştırmak (alternatif)

```bash
pip install -r requirements.txt
export WISHLIST_URLS="https://www.amazon.com.tr/hz/wishlist/ls/XXXXXXXX"
export TELEGRAM_BOT_TOKEN="..."  TELEGRAM_CHAT_ID="..."
python stock_watcher.py --test-notify   # bildirim testi
python stock_watcher.py                 # stok kontrolü
```

Windows'ta `export` yerine `set` kullanın. Düzenli çalıştırmak için Windows Görev Zamanlayıcı'yı veya macOS/Linux'ta `cron`'u kullanabilirsiniz.
