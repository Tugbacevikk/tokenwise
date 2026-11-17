# Barındırılan Tokenwise kurulumu

Bu sürüm bir GitHub App olarak çalışır:

- Varsayılan dala yapılan her push'ta depo taranır (RepoDoctor) ve sağlık geçmişine eklenir.
- Her pull request'e, taslaklar dahil, bir risk raporu yorumu yazılır (ReleaseGuard). Sonraki push'larda yeni yorum açılmaz, aynı yorum güncellenir.
- Panelde bir bulgu için "AI ile düzelt" denince Agent bir değişiklik hazırlar. Organization sahibi onaylarsa değişiklik `tokenwise/fix-N` dalına push edilir ve taslak PR açılır. Ana dala hiçbir zaman yazılmaz.

Parçalar:

| Servis | Komut | Görevi |
| --- | --- | --- |
| `db` | PostgreSQL 16 | Bütün veri ve iş kuyruğu (Redis gerekmez) |
| `web` | `tokenwise server web` | Webhook'lar, GitHub ile giriş, panel ve API |
| `worker` | `tokenwise server worker` | Tarama, PR yorumu, düzeltme önerisi ve push |

## 1. GitHub App oluşturma

GitHub'da **Settings → Developer settings → GitHub Apps → New GitHub App** yolunu izleyin.

- **Homepage URL:** `https://tokenwise.example.com`
- **Callback URL:** `https://tokenwise.example.com/auth/callback`
- **Webhook URL:** `https://tokenwise.example.com/webhooks/github`
- **Webhook secret:** rastgele uzun bir değer (`GITHUB_WEBHOOK_SECRET`)
- **Repository permissions:**
  - Contents: *Read and write* (klonlamak ve `tokenwise/fix-N` dalını push etmek için)
  - Pull requests: *Read and write* (taslak PR açmak için)
  - Issues: *Read and write* (PR yorumu yazmak için)
  - Metadata: *Read-only*
- **Subscribe to events:** Push, Pull request (Installation olayları zaten gelir)

App'i oluşturduktan sonra şunları not edin:

- App ID
- Client ID
- Client secret (yeni bir tane üretin)
- Private key: bir `.pem` dosyası indirilecek.

## 2. Sunucu

Docker ve Docker Compose kurulu bir Linux makine gerekir.

```bash
git clone https://github.com/Tugbacevikk/tokenwise && cd tokenwise
cp .env.server.example .env.server        # doldurun
cp ~/Downloads/<app>.private-key.pem github-app.pem
chmod 600 github-app.pem .env.server

# docker compose'un kendi değişkenleri (.env):
echo "POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" > .env
echo "DOCKER_GID=$(stat -c %g /var/run/docker.sock)" >> .env

# worker'ın çalışma klasörü: hem host'ta hem konteynerde aynı yol
sudo mkdir -p /var/lib/tokenwise/work && sudo chown 10001:10001 /var/lib/tokenwise/work

docker pull python:3.12-slim              # kontrollerin çalışacağı imaj
docker compose up -d --build
curl -s localhost:8000/healthz            # {"ok": true, "jobs": {...}}
```

Önüne TLS veren bir ters vekil (proxy) koyun. Caddy ile tek satır yeter:

```
tokenwise.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

`web` açılışta tabloları kendisi oluşturur. Elle yapmak için `tokenwise server migrate` kullanılabilir.

## 3. İlk kullanım

1. GitHub App sayfasından **Install** deyin ve bir hesap ya da organization seçin. Kuran kişi Tokenwise'ta o organization'ın **sahibi** olur. Kişisel hesap, tek üyeli bir organization sayılır.
2. Worker depoları ve varsayılan dallarını okur, her birini bir kez tarar.
3. `https://tokenwise.example.com` adresinden **GitHub ile giriş yap** deyin. Aynı kuruluma erişimi olan diğer kişiler giriş yaptığında **üye** olarak eklenir.
4. Düzeltme isteyebilmek için sahip, **Model ayarları** bölümünden bir model anahtarı girer. Tarama ve risk raporu model çağırmaz, bu yüzden anahtar gerektirmez.

## Güvenlik notları

- **Depodaki `tokenwise.toml` güvenilmez girdi sayılır.** Sunucu bu dosyadan yalnızca `check`, `check_timeout` (en fazla 900 s), `extensions`, `ignore_dirs`, `focus_lines` ve `max_files` değerlerini alır. Model sağlayıcısını, adresini, sandbox'ı, imajı ve ağ ayarını sunucu belirler.
- **Kontrol komutu yalnızca konteynerde çalışır.** Konteynerde ağ kapalıdır, sistem salt okunurdur, hiçbir yetki (capability) verilmez ve CPU, bellek ve süreç sınırları uygulanır. Konteyner deponun geçici kopyasını görür. Docker ulaşılamazsa kontrol "başarısız" değil "atlandı" sayılır; öneri bu bilgiyle gösterilir.
- **Docker soketine erişim, host'ta root yetkisine denktir.** Worker'ı yalnızca bu iş için ayrılmış bir makinede ya da sanal makinede çalıştırın. Daha sıkı izolasyon için Docker'ı [rootless](https://docs.docker.com/engine/security/rootless/) modda ya da gVisor (`--runtime=runsc`) ile kullanın.
- **Model anahtarları** `TOKENWISE_SECRET_KEY` ile şifrelenmiş olarak saklanır ve API hiçbir zaman geri döndürmez. Bu anahtarı değiştirirseniz kayıtlı anahtarlar okunamaz hale gelir; sahiplerin anahtarı yeniden girmesi gerekir.
- **GitHub tokenları** bellekte, en fazla bir saatlik ömürleriyle tutulur. Git'e tek komutluk bir HTTP başlığı olarak verilir; uzak adrese ya da `.git/config` dosyasına yazılmaz. Hata mesajlarında maskelenir.
- **Her API isteğinde üyelik kontrol edilir.** Başka bir organization'a ait kayıtlar "bulunamadı" (404) olarak döner. Düzeltmeyi onaylamak, reddetmek ve model ayarını değiştirmek için sahip olmak gerekir. Bir şeyi değiştiren istekler `X-Tokenwise: 1` başlığını taşımak zorundadır, böylece başka sitelerden gönderilen formlar işe yaramaz.
- **App kaldırılınca** o organization'ın bütün verisi silinir: depolar, bulgular, PR raporları, düzeltmeler, maliyet kayıtları ve işler.

## Değişkenler

| Değişken | Zorunlu | Açıklama |
| --- | --- | --- |
| `DATABASE_URL` | evet | `postgresql://kullanıcı:şifre@host:5432/db` |
| `TOKENWISE_SECRET_KEY` | evet | En az 32 karakter; model anahtarlarını şifreler |
| `TOKENWISE_PUBLIC_URL` | evet | Panelin adresi |
| `GITHUB_APP_ID`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_WEBHOOK_SECRET` | evet | GitHub App bilgileri |
| `GITHUB_PRIVATE_KEY` ya da `GITHUB_PRIVATE_KEY_PATH` | evet | App'in PEM anahtarı |
| `TOKENWISE_SANDBOX_IMAGE` | hayır | Kontrol imajı; varsayılan `python:3.12-slim` |
| `TOKENWISE_SCAN_TOOLS` | hayır | Varsayılan `ruff,bandit`; ayrıca `coverage` (depodaki `coverage.json` / `coverage.xml` okunur) |
| `TOKENWISE_ALLOWED_MODEL_URLS` | hayır | Sahiplerin seçebileceği OpenAI uyumlu adresler; boşsa yalnızca Anthropic kullanılabilir |
| `TOKENWISE_WORK_DIR` | worker | Klonlar ve kontrol kopyaları için klasör. Docker host'unda da aynı yolda olmalı |

## Ölçek

- Worker sayısını artırmak için: `docker compose up -d --scale worker=3`. İşler `FOR UPDATE SKIP LOCKED` ile alındığı için aynı işi iki worker almaz.
- Yarıda kalan bir iş (worker çöktüyse) 2 saat sonra kuyruğa geri döner.
- Başarısız iş artan aralıklarla 3 kez denenir. Aynı işin daha yeni hali kuyruktaysa eskisi tekrar denenmez.
- Aynı dala art arda gelen push'lar tek tarama olur; aynı PR'a art arda gelen push'lar da tek rapor olur.
