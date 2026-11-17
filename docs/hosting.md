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

Kurulumu kontrol etmek için:

```bash
docker compose exec worker tokenwise server doctor
```

Komut sırasıyla şunları dener: veritabanı bağlantısı ve tablolar, gizli değerler, GitHub App (anahtarla imzalanmış bir istek gönderip olay aboneliklerini ve izinleri okur), Docker, kontrol imajı ve çalışma klasörünün konteynerden görünüp görünmediği. Bir adım başarısız olursa ne yapılması gerektiğini de yazar.

## 3. İlk kullanım

1. GitHub App sayfasından **Install** deyin ve bir hesap ya da organization seçin. Kuran kişi Tokenwise'ta o organization'ın **sahibi** olur. Kişisel hesap, tek üyeli bir organization sayılır.
2. Worker depoları ve varsayılan dallarını okur, her birini bir kez tarar.
3. `https://tokenwise.example.com` adresinden **GitHub ile giriş yap** deyin. Aynı kuruluma erişimi olan diğer kişiler giriş yaptığında **üye** olarak eklenir.
4. Düzeltme isteyebilmek için sahip, **Model ayarları** bölümünden bir model anahtarı girer. Tarama ve risk raporu model çağırmaz, bu yüzden anahtar gerektirmez.

## Canlı deneme (beta'dan önce, kendi bilgisayarında)

Sunucu kiralamadan önce bütün akışı kendi makinende, geçici bir HTTPS tüneliyle deneyebilirsin. Windows'ta bunu WSL 2 içinde, Docker Desktop'ın WSL entegrasyonu açıkken yap.

1. **Tünel aç:** `cloudflared tunnel --url http://localhost:8000` komutunu çalıştır. Çıktıdaki `https://….trycloudflare.com` adresini not et. Tünel her açılışta yeni bir adres verir; adres değişirse 2. ve 3. adımları tekrarla.
2. **GitHub App oluştur:** 1. bölümdeki ayarlarla bir "Tokenwise Deneme" App'i oluştur. Callback ve webhook adreslerine tünel adresini yaz.
3. **Ayarları gir:** `.env.server` dosyasında `TOKENWISE_PUBLIC_URL` olarak tünel adresini yaz. Sonra `docker compose up -d --build` ve `docker compose exec worker tokenwise server doctor` komutlarını çalıştır. Bütün satırlar `ok` olmalı.
4. **Deneme deposu oluştur:** Kendi hesabında boş, özel bir depo aç ve içine `app.py` adlı bir dosya ekle:

   ```python
   import subprocess


   def run(cmd):
       return subprocess.call(cmd, shell=True)
   ```

   Aynı depoya şu içerikte bir `tokenwise.toml` ekle:

   ```toml
   [tokenwise]
   check = "{python} -c 'import app'"
   ```

5. App'i yalnızca bu depoya kur ve aşağıdaki tabloyu sırayla dene:

| # | Ne yapılır | Beklenen sonuç |
| --- | --- | --- |
| 1 | App'i kur | Birkaç saniye içinde panelde depo görünür; sağlık skoru ve "subprocess.call kabukla komut çalıştırıyor" bulgusu çıkar |
| 2 | Panelde **GitHub ile giriş yap** | Sol üstte kullanıcı adın ve "Sahip" yazar |
| 3 | `main`'e bir commit push et | Kısa süre sonra **Şimdi tara** gerekmeden yeni tarama gelir; sağlık grafiğine bir çubuk eklenir |
| 4 | Bir dal aç, değişiklik yap, **taslak** PR aç | PR'a "ReleaseGuard risk raporu" yorumu gelir |
| 5 | Aynı dala bir commit daha push et | Yeni yorum açılmaz; mevcut yorumun commit kısmı güncellenir |
| 6 | **Model ayarları** bölümüne Anthropic anahtarını gir, bulguda **AI ile düzelt** de | "Düzeltmeler" sekmesinde öneri ve diff görünür; kontrol "geçti" yazar |
| 7 | **Onayla ve taslak PR aç** | GitHub'da `tokenwise/fix-1` dalından taslak PR açılır; `main` değişmez |
| 8 | Bir bulguyu **Yok say**, sonra **Şimdi tara** | Bulgu "Yok sayılan" listesinde kalır, geri açılmaz |
| 9 | App'i depodan kaldır | Panelde organization kaybolur; `docker compose exec db psql -U tokenwise -c "select count(*) from repositories"` komutu 0 döner |

Bir adım beklendiği gibi çalışmazsa `docker compose logs worker --tail 50` çıktısını kaydet.

## Güvenlik notları

- **Depodaki `tokenwise.toml` güvenilmez girdi sayılır.** Sunucu bu dosyadan yalnızca `check`, `check_timeout` (en fazla 900 s), `extensions`, `ignore_dirs`, `focus_lines` ve `max_files` değerlerini alır. Model sağlayıcısını, adresini, sandbox'ı, imajı ve ağ ayarını sunucu belirler.
- **Kontrol komutu yalnızca konteynerde çalışır.** Konteynerde ağ kapalıdır, sistem salt okunurdur, hiçbir yetki (capability) verilmez ve CPU, bellek ve süreç sınırları uygulanır. Konteyner deponun geçici kopyasını görür. Docker ulaşılamazsa kontrol "başarısız" değil "atlandı" sayılır; öneri bu bilgiyle gösterilir.
- **Docker soketine erişim, host'ta root yetkisine denktir.** Bu yüzden worker'ı yalnızca bu iş için ayrılmış bir makinede ya da sanal makinede çalıştırın. Önerilen yerleşim için aşağıdaki "Üretim yerleşimi" bölümüne bakın.
- **Kontrol hiçbir zaman root olarak çalışmaz.** Konteyner, worker'ın kendi kullanıcısıyla çalışır. Worker root olarak çalışıyorsa konteyner `nobody` (65534) kullanıcısına düşer. Açık dosya sayısı 1024 ile sınırlıdır.
- **Model anahtarları** `TOKENWISE_SECRET_KEY` ile şifrelenmiş olarak saklanır ve API hiçbir zaman geri döndürmez. Bu anahtarı değiştirirseniz kayıtlı anahtarlar okunamaz hale gelir; sahiplerin anahtarı yeniden girmesi gerekir.
- **GitHub tokenları** bellekte, en fazla bir saatlik ömürleriyle tutulur. Git'e tek komutluk bir HTTP başlığı olarak verilir; uzak adrese ya da `.git/config` dosyasına yazılmaz. Hata mesajlarında maskelenir.
- **Her API isteğinde üyelik kontrol edilir.** Başka bir organization'a ait kayıtlar "bulunamadı" (404) olarak döner. Düzeltmeyi onaylamak, reddetmek ve model ayarını değiştirmek için sahip olmak gerekir. Bir şeyi değiştiren istekler `X-Tokenwise: 1` başlığını taşımak zorundadır, böylece başka sitelerden gönderilen formlar işe yaramaz.
- **App kaldırılınca** o organization'ın bütün verisi silinir: depolar, bulgular, PR raporları, düzeltmeler, maliyet kayıtları ve işler.

## Üretim yerleşimi

Worker müşteri kodunu (projenin kontrol komutunu) çalıştıran tek parçadır. Bu yüzden ayrı bir güven sınırı içinde tutulur:

| Makine | Üzerinde çalışan | Erişimi |
| --- | --- | --- |
| Web makinesi | `web`, PostgreSQL | İnternete açık (443). Docker soketi yok. |
| Worker sanal makinesi (bir ya da daha fazla) | `worker`, Docker | Yalnızca PostgreSQL'e ve GitHub'a çıkış. İçeriden gelen bağlantı yok. Başka bir hizmet çalıştırılmaz. |

- **gVisor:** Kontroller ile host çekirdeği arasına kullanıcı alanında çalışan bir çekirdek koyar; bir konteynerden kaçış ihtimalini büyük ölçüde azaltır. Kurulum şöyle:

  ```bash
  # worker makinesinde
  curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor.gpg
  echo "deb [signed-by=/usr/share/keyrings/gvisor.gpg] https://storage.googleapis.com/gvisor/releases release main" \
    | sudo tee /etc/apt/sources.list.d/gvisor.list
  sudo apt update && sudo apt install -y runsc
  sudo runsc install && sudo systemctl restart docker
  ```

  Güncel kurulum adımları için: https://gvisor.dev/docs/user_guide/install/ . Ardından `.env.server` dosyasına `TOKENWISE_SANDBOX_RUNTIME=runsc` yazın. `tokenwise server doctor` komutu bu runtime'ın Docker'da kayıtlı olup olmadığını kontrol eder.
- **Rootless Docker:** Docker servisi de root olmadan çalışabilir. Böylece soket ele geçirilse bile host'ta root yetkisi kazanılmaz. Rootless kurulumda `/var/run/docker.sock` yerine kullanıcının soket yolu worker'a bağlanır.
- **Veritabanı kullanıcısı:** Worker için yalnızca Tokenwise tablolarına erişebilen ayrı bir PostgreSQL kullanıcısı açın; süper kullanıcı vermeyin.

## Güvenilirlik

- **Webhook tekrarları:** Her teslimatın `X-GitHub-Delivery` kimliği, olayın etkileriyle aynı transaction içinde kaydedilir. GitHub aynı olayı yeniden gönderirse hiçbir şey yapılmaz. Yarıda kalan bir teslimat ise yeniden işlenir. Kayıtlar 30 gün tutulur.
- **Sıra dışı olaylar:** Push ve PR işleri olayın zamanını taşır. Kuyrukta bekleyen iş her zaman en yeni girdiyi tutar. Daha eski bir olay geç gelirse atlanır.
- **Aynı anda çalışmama:** Aynı deponun taramaları, aynı PR'ın raporu ve aynı düzeltme hiçbir zaman eşzamanlı çalışmaz.
- **Kalp atışı (heartbeat):** Çalışan iş her dakika "hâlâ çalışıyorum" kaydı bırakır. 10 dakika boyunca kayıt gelmezse worker'ın düştüğü varsayılır ve iş kuyruğa geri döner.
- **Güvenli yeniden deneme:** Yeniden denenen bir iş aynı PR yorumunu ve aynı taslak PR'ı bulup kullanır; ikinci bir kopya açmaz.
- **Güncel olmayan öneri:** Onay verildiğinde öneri, ana dalın o anki haliyle karşılaştırılır. Bu arada dosya değiştiyse düzeltme "Güncel değil" durumuna geçer ve yeni öneri istenir.
- **Oturumlar:** Bir oturum 24 saat sürer. Üyelik her girişte GitHub'dan yeniden okunur. GitHub organization'ından çıkarılan biri en geç bir gün içinde erişimini kaybeder. "Tüm cihazlarda çıkış yap" seçeneği kişinin bütün oturumlarını kapatır.

## Yedekleme

`docker compose up` komutu `backup` servisini de başlatır. Bu servis her gün bir `pg_dump` alır ve `./backups` klasörüne yazar. Her yedek `pg_restore --list` ile okunabilirliği doğrulandıktan sonra kaydedilir ve 14 günden eski olanlar silinir. Bozuk bir yedek, sağlam olanların yerine geçmez.

- **Hemen bir yedek almak için:** `docker compose run --rm -e BACKUP_EVERY=0 backup`
- **Makine dışına kopya:** Yedekler aynı diskte durdukça makineyle birlikte kaybolabilir. `./backups` klasörünü her gün başka bir yere kopyalayın (örneğin `rclone sync ./backups remote:tokenwise-backups`).
- **Geri yükleme:**

  ```bash
  docker compose stop web worker
  docker compose exec -T db dropdb -U tokenwise tokenwise
  docker compose exec -T db createdb -U tokenwise tokenwise
  docker compose exec -T db pg_restore -U tokenwise -d tokenwise --no-owner < backups/tokenwise-YYYYMMDD-HHMMSS.dump
  docker compose start web worker
  ```

  Geri yüklenen veritabanındaki model anahtarları yalnızca aynı `TOKENWISE_SECRET_KEY` ile okunabilir. Bu anahtarı yedeklerden ayrı bir yerde saklayın.
- **Geri yüklemeyi deneyin:** Ayda bir, bir yedeği boş bir veritabanına geri yükleyip `tokenwise server doctor` komutunu çalıştırın. Hiç denenmemiş bir yedeğe güvenmeyin.

## İzleme

`TOKENWISE_METRICS_TOKEN` tanımlıysa `GET /metrics` adresi Prometheus formatında şu metrikleri verir (istek `Authorization: Bearer <token>` başlığı taşımalıdır):

- iş türüne ve durumuna göre iş sayısı,
- son bir saatte başarısız olan işler,
- ortalama iş süresi,
- en eski bekleyen işin bekleme süresi,
- son bir saatte gelen webhook sayısı,
- düzeltmelerin durumları.

Önerilen alarmlar:

- `tokenwise_oldest_queued_job_seconds > 600`: worker yetişemiyor ya da çalışmıyor.
- `tokenwise_jobs_failed_last_hour > 5`: GitHub ya da Docker tarafında bir sorun var.

## Değişkenler

| Değişken | Zorunlu | Açıklama |
| --- | --- | --- |
| `DATABASE_URL` | evet | `postgresql://kullanıcı:şifre@host:5432/db` |
| `TOKENWISE_SECRET_KEY` | evet | En az 32 karakter; model anahtarlarını şifreler |
| `TOKENWISE_PUBLIC_URL` | evet | Panelin adresi |
| `GITHUB_APP_ID`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_WEBHOOK_SECRET` | evet | GitHub App bilgileri |
| `GITHUB_PRIVATE_KEY` ya da `GITHUB_PRIVATE_KEY_PATH` | evet | App'in PEM anahtarı |
| `TOKENWISE_SANDBOX_IMAGE` | hayır | Kontrol imajı; varsayılan `python:3.12-slim` |
| `TOKENWISE_SANDBOX_RUNTIME` | hayır | `runsc` = gVisor; boşsa Docker'ın varsayılanı |
| `TOKENWISE_METRICS_TOKEN` | hayır | `/metrics` için bearer token; boşsa bu adres yok |
| `TOKENWISE_SCAN_TOOLS` | hayır | Varsayılan `ruff,bandit`; ayrıca `coverage` (depodaki `coverage.json` / `coverage.xml` okunur) |
| `TOKENWISE_ALLOWED_MODEL_URLS` | hayır | Sahiplerin seçebileceği OpenAI uyumlu adresler; boşsa yalnızca Anthropic kullanılabilir |
| `TOKENWISE_WORK_DIR` | worker | Klonlar ve kontrol kopyaları için klasör. Docker host'unda da aynı yolda olmalı |

## Ölçek

- Worker sayısını artırmak için: `docker compose up -d --scale worker=3`. İşler `FOR UPDATE SKIP LOCKED` ile alındığı için aynı işi iki worker almaz.
- Worker çökerse yarıda kalan iş 10 dakika sonra kuyruğa geri döner (bkz. Güvenilirlik).
- Başarısız iş artan aralıklarla 3 kez denenir. Aynı işin daha yeni hali kuyruktaysa eskisi tekrar denenmez.
- Aynı dala art arda gelen push'lar tek tarama olur; aynı PR'a art arda gelen push'lar da tek rapor olur.
