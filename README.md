# MAVE – AI Search Engine (v0.10)

Barbaacha AI'n deeggarame: **index mataa (crawler + PostgreSQL full-text)** + **web search** + **deebii AI maddoo waliin** + Android app.

## Qabiyyee
```
android/   Kotlin + Jetpack Compose: `app/` (UI + ViewModel) + `data/` (network, storage, API models; Gradle module `:data`)
backend/   FastAPI + PostgreSQL (full-text search: tsvector + GIN) search engine
backend/web/   Web app + Admin dashboard (HTML/CSS/JS qulqulluu, build hin barbaachisu) — `/web/` irratti tajaajilama
ops/       Caddy (HTTPS), Prometheus + alert rules (alerts.yml), backup/restore scripts; docker-compose.prod.yml
PLAN_STATUS.md   Master Plan v1.0 haala guutuu + itti aanee maal akka hojjatamu
```

## 1. Backend jalqabuu
Database: **PostgreSQL ≥ 13** (`MAVE_DATABASE_URL=postgresql://user:pass@host:5432/db`; durtii local: `postgresql://mave:mave@localhost:5432/mave`). Tables fi index ofumaan uumama (migration `schema_migrations` keessatti hordofama; worker hedduu yeroo tokko eegalan iyyuu hanga tokko qofa hojjata). Extension `unaccent` ni fayyadama (database abbaa isaatiin uumama; super-user hin barbaachisu).
```
cd backend
pip install -r requirements.txt
docker compose up -d db          # PostgreSQL local (ykn database kee ofii fayyadami)
# backend/.env duraanuu jira (XAI_API_KEY + secret random waliin; git irraa dhoksame). Haaraa barbaadde: cp .env.example .env
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Docker (API + PostgreSQL waliin): `docker compose up --build` (backend/ keessatti).

### SQLite (mave.db durii) → PostgreSQL
Data durii yoo qabaatte: `python migrate_sqlite.py mave.db` (target = `MAVE_DATABASE_URL`, duwwaa ta'uu qaba). Users, sessions, history, saved, documents, frontier, seeds, audit log hundi ni dabarsa; sequence'oota sirreessa; FK-orphan fi URL dheeraa (>2000 byte) ni dhiisa; full-text index PostgreSQL ofumaan ijaara. Bu'aa: tableen tokkoon tokkoon "copied/skipped" ni agarsiisa.
Backup (prod): `ops/backup_postgres.sh` (pg_dump, guyyaa 14). **Restore/test (v0.10.4)**: `ops/restore_postgres.sh backups/mave-YYYY-MM-DD.dump --verify` database yeroo-gabaabaa (`mave_verify`) keessatti deebisee tarree ni lakkaa'a, database dhugaa hin tuqu — ji'a ji'aan guyyaa tokko deemsisi. Database dhugaa bakka buusuuf `MAVE_RESTORE_CONFIRM=mave` barbaachisa; dogoggora yoo ta'e API cufamee hafa (database walakkaa deebi'uu danda'a).
Key hin kaa'in taanaan demo data/demo answer ni deebisa.

### AI deebii (xAI Grok)
`XAI_API_KEY` yoo kaa'ame `/ai/answer` Grok fayyadama (`MAVE_XAI_MODEL`, durtii `grok-4.6`; docs.x.ai irratti fakkeenyi amma `grok-4.7` agarsiisa, `grok-4.6` yoo hin argamne env kana jijjiiri). `MAVE_AI_PROVIDER=xai|anthropic` yoo kenname isa tokko qofa fayyadama (isa kaan irratti hin jijjiiramu); `auto` = key jiru filata, xAI dura.
- Grok'tti maddoota MAVE ofii (local index + Brave) qofa ergama. Live search hin eegalu (`tools` hin ergamu) fi `store:false` waan ta'eef xAI gaaffii kee guyyaa 30 hin kuusu.
### AI deebii: citations fi eegumsa (Phase 5)
`POST /api/v1/ai/answer {query, lang}` → `{answer, sources, citations:[{n,title,url}], grounded, insufficient}`
- `[n]` hunduu `sources[n-1]` ti; lakkoofsi hin jirre (fkn `[9]`) deebii keessaa ni haqama. `citations` kan dhugaan wabeeffaman qofa.
- `insufficient: true` → maddoota keessatti deebiin hin argamne (model `NO_ANSWER_IN_SOURCES` jedhe, ykn maddi hin jiru); provider hin waamamu yoo maddi hin jiru; ergaan Afaan Oromoo/English/... ni deebi'a.
- `grounded: false` → deebii citation tokkollee hin qabne (Android yeroo ammaa kana hin agarsiisu).
- Eegumsa: maddoonni URL `http(s)` qofa, markup/control/bidi characters ni haqamu, barreeffamni "ignore previous instructions" fakkaatu model'tti hin ergamu (URL qofa hafa); prompt keessatti maddoonni code random (yeroo hundaa haaraa) waliin cufamu, kanaaf fuula tokko "sources xumurame" jedhee hin sobu; deebii keessaa fakkii, link, URL, HTML ni haqamu (data baafachuu irraa ittisa).
- Sanyii eegumsa kanaa heuristic dha; qabxiin ijoo isa lamaffaa fi sadaffaa (code random + validation deebii) dha.
- Deebii dhugaa argachuuf `BRAVE_API_KEY` ykn index crawler kee barbaachisa; Brave malee maddoonni demo qofa dha.
- xAI yoo kufe, client'tti `502 AI provider error <code>` qofa deebi'a; ibsa guutuun log `mave.ai` keessatti (furtuun gonkumaa hin barreeffamu).

### Index mataa kee ijaaruu (MAVE'n Google irraa adda kan taasisu)
```
python crawl.py seeds.txt --pages 500 --depth 2 --delay 1.0
# ykn server irraa:
curl -X POST localhost:8000/admin/crawl -H "X-Admin-Key: $MAVE_ADMIN_KEY" \
     -H "content-type: application/json" -d '{"seeds":["https://om.wikipedia.org/"],"max_pages":300}'
curl localhost:8000/admin/stats -H "X-Admin-Key: $MAVE_ADMIN_KEY"
```
Crawler v1.0 (`app/crawler/` package): robots.txt (`Disallow` + `Crawl-delay`) ni kabaja, domain tokkoof yeroo eeggata, domain seed qofa keessa hafa (`www.` ykn hin qabne tokkuma), `seeds.txt` jijjiiri.
```
python crawl.py seeds.txt --pages 500 --depth 2 --sitemaps     # robots.txt Sitemap: + /sitemap.xml (index, .gz) dabalata
python crawl.py seeds.txt --recrawl                            # fuulota yeroon isaanii gahe deebi'ee fiduu
```
- **Frontier**: priority queue (seed 100 > sitemap 60-80 > link 10/(depth+1)), crawl rakkate itti fufee (resume).
- **Retry + backoff**: dogoggora yeroo muraasaaf (timeout, reset, 408/425/429/5xx) 30s → 60s → 120s ... (`Retry-After` ni kabajama, hanga 3 yaalii); dogoggora dhaabbataa (403, non-HTML ...) battalumatti `failed`. 404/410 → fuulli index irraa haqama (`gone`).
- **Redirect + canonical**: redirect booda URL xumuraa irratti index godha; `<link rel=canonical>` domain tokko keessatti qofa kabajama (domain biraatti kan agarsiisu ni dhiisama); fuulli URL jijjiirama (`?ref=1`) fuula duraan index ta'e irratti hin barreeffamu. `<base href>`, `<meta name=robots content=noindex|nofollow|none>` ni kabajamu.
- **Duplicate**: dhugaa (SHA-256) + near-duplicate (SimHash 64-bit, Hamming ≤3; fuula token ≥100 qofaaf). `--near-dup 0` yoo cufte.
- **Recrawl**: fuulli hunduu yeroo mataa isaa qaba (jalqaba guyyaa 7); jijjiirame → walakkaa (min 6 sa'aa), jijjiiramne → dachaa (max guyyaa 60). `/admin/crawl` ammo sitemap + recrawl ni hojjata.
- Dogoggora gulaaluuf: `GET /admin/stats` keessatti `frontier_failed|blocked|duplicate|gone|retrying`, `documents_due_recrawl`.
Ranking: PostgreSQL `ts_rank_cd` (title A ≈ ×6 body B; accent hin dhimma, `unaccent`)  + title match + link popularity + freshness + domain diversity → Brave results waliin RRF fusion.

### Web app (Phase 8) fi Admin dashboard (Phase 9) — v0.8
Backend eegalee booda: **Web app** `http://localhost:8000/web/` · **Admin** `http://localhost:8000/web/admin.html` (`/` ofumaan `/web/` tti geessa).
- Web: barbaacha (Web/News/Images/Videos/Tech), **AI deebii + citation lakkoofsaan (`[1]` → link maddoo)**, "ragaa gahaa hin jiru" ifatti, infinite scroll (page ≤ 20), autocomplete, "Did you mean", sagalee (browser deeggaruu qofa), login/signup/forgot+reset/verify email, saved + history (**offline**: localStorage + service worker; yoo seente server waliin walitti makama), Afaan Oromoo/English, light/dark/auto, account delete, logout hunda irraa.
- Eegumsa: CSP cimaa (`script-src 'self'`, inline script/style hin jiru, `unsafe-eval` hin jiru); barreeffamni hundi `textContent` (HTML hin ta'u); URL `http(s)` qofa link ta'a; access token **memory qofa**, refresh token localStorage (CSP cimaa XSS ni xiqqeessa; garuu XSS jiraate argamuu danda'a — kana yaadadhu); provider key gonkumaa client keessa hin jiru (test ni mirkaneessa).
- E-mail link: `MAVE_PUBLIC_URL=https://keessan.org/web/#` kaa'i; link `…/web/#/reset-password?token=…` ta'a (hash route).
- Admin: `X-Admin-Key` (tab sessionStorage qofa). Overview (health: version, schema, provider hojjachuu — key hin agarsiisu; index stats), Crawler (seed manage, crawl jalqabuu, crawl errors), Analytics (query/guyyaa, top query, top domains, afaan), Users (barbaaduu, session cufuu), Audit log (gochi admin hundi).
- **Admin user-level (v0.11.4)**: admin = `X-Admin-Key` (break-glass; admin jalqabaa itti muudama) **ykn** user `role='admin'` kan Bearer access token isaatiin (`Authorization: Bearer <token>`) fayyadamu. Role fi `disabled` request hunda irratti DB irraa ilaalama → role haquun/account cufuun battalumatti hojjata. `POST /api/v1/admin/users/{id}/role` `{"role":"admin"|"user"}`; namni kamiyyuu role ofii hin jijjiiru, ofii hin cufu (admin key qofa sirreessa). Audit log keessatti `actor` = `admin-key` ykn `user:<id>`. Dashboard (`admin.html`) amma key qofaan seena; Users tab irratti Role fi "Make admin / Remove admin" jira. Admin jalqabaa: `curl -X POST .../admin/users/<id>/role -H "X-Admin-Key: $MAVE_ADMIN_KEY" -H 'content-type: application/json' -d '{"role":"admin"}'`.
- API admin haaraa (`/api/v1/admin/...`): `health`, `analytics`, `crawl-errors`, `seeds` (GET/POST/PUT/DELETE), `users`, `users/{id}/revoke-sessions`, `users/{id}/disable|enable|role`, `audit`, `metrics` (Prometheus; `X-Admin-Key` ykn `Authorization: Bearer <key>`). `POST /admin/crawl` amma `seeds` duwwaa yoo ta'e seed dashboard keessa jiru fayyadama.
- Alert (v0.10.4): `ops/alerts.yml` (API cabe, 5xx>5%, /search suuta>1.5s, crawler fail>30%, recrawl backlog, index duwwaa). Gatiin kun **jalqaba qofa** — torban tokko traffic dhugaa booda sirreessi. `tests/test_ops.py` metric hundi API irraa baha jiru mirkaneessa. `promtool check rules ops/alerts.yml` hin deemne.
- Load test (v0.10.4): `python loadtest.py --base-url http://127.0.0.1:8000 --concurrency 20 --duration 30 --max-p95-ms 800 --max-error-rate 0.01` (stdlib qofa). Server kee qofa irratti; IP public ta'e `--i-own-this-server` barbaada. Staging irratti `MAVE_RATE_LIMIT` ol-aanaa godhi, yoo kana ta'uu baate 429 baay'inaan argama.
- Monitoring: `/admin/metrics` (request/error/latency, route template qofa — query string hin jiru), log JSON (`MAVE_LOG_FORMAT=json`, production keessatti durtii; `mave.access` — query string hin barreeffamu), `ops/prometheus.yml`.
- Migration v4: `seeds`, `audit_log`.

### Endpoints
**API v1**: hundi `/api/v1/...` jalatti ni jira (fkn `GET /api/v1/search`); karaa duraa (`/search`, ...) clients 0.2 tiif ni hafe.
`GET /search?q=&type=web|tech|news|images|videos` · `GET /suggest?q=` · `GET /trending` · `POST /ai/answer` · `GET /me`
`POST /auth/register|login` · `GET/POST/DELETE /me/history` · `GET/POST/DELETE /me/saved`
`POST /admin/crawl` · `GET /admin/stats|health|analytics|crawl-errors|seeds|users|audit|metrics` · `POST /admin/users/{id}/revoke-sessions|disable|enable|role` · `GET /health`

### Test
PostgreSQL barbaachisa: `export MAVE_TEST_DATABASE_URL=postgresql://mave:mave@localhost:5432/postgres` (role `CREATEDB` qabu). Test hundi database ofii (`mave_test_*`) argata, xumuraan ni haqama.
`python -m unittest discover -s tests -t . -v` (v0.9 + PostgreSQL: 215 tests, v0.10: +28 test PostgreSQL malee `tests/test_search_core.py` + 1 test PostgreSQL `test_oromoo_stem_search`; `test_crawler_v1.py` 40 ofii isaa, offline; API test fi provider test FastAPI/httpx install godhamee qofa — `pip install -r requirements.txt` booda hundi ni hojjata)
Web app/Admin: `node --test tests/web/core.test.js tests/web/smoke.test.js` (Node ≥ 18; npm install hin barbaachisu; `app.js` fi `admin.js` DOM shim irratti dhugumaan ni hojjatu)

## 2. Android
1. `android/` Android Studio (Koala+) keessatti bani → Gradle sync → Run (emulator).
   Moduleen lama: `:app` (`ui/` feature-tti: Onboarding/Auth/Home/Results/Library/Profile screens, `MaveViewModel`, `MaveApplication`) → `:data` (`Network`, `Session`, `MaveApi`, models, `LocalStore`, `SecureStore`). `:data` URL hin beeku: `MaveApplication` `Network.configure(BuildConfig.API_BASE_URL)` ni waama.
2. Debug build `http://10.0.2.2:8000/` fayyadama. Release: URL dhugaa `-PMAVE_API_URL=https://api.keessan.org/` (ykn `android/gradle.properties`) kenni; HTTPS ykn URL jiru hin kennin yoo ta'e release build ni fashaaleta.
3. Bilbila dhugaa irratti: `10.0.2.2` bakka IP kompiitara keetiin bay'ii.

Amaloota: onboarding, login/signup (token encrypted), autocomplete, "Did you mean", AI answer + sources,
Web/Images/Videos/News/Tech, voice search, saved + history (offline, DataStore) fi server waliin sync,
dark/light, Afaan Oromoo + English, error + retry.
**Haaraa (v0.9, Phase 7 jalqaba)**: *Infinite scroll* — Web/News/Tech/Videos/Images tab hundumaa gad-bu'uu irratti fuula itti aanu ofumaan fida (`GET /search?page=N`, hanga fuula 20; URL walfakkaatu ni haqama; fe'uun yoo kufe bu'aan jiru hin bada, "Irra deebi'i" qofa mul'ata). *AI deebii*: lakkoofsi `[n]` maddoo wabeeffame qofa agarsiisa (`citations`); deebiin citation hin qabne (`grounded:false`) akeekkachiisa argata; `insufficient:true` yoo ta'e maddoonni hin agarsiifaman. Qorannoon kun Android Studio / GitHub Actions irratti compile godhamee mirkanaa'uu qaba (kompiitara kana irratti Kotlin compiler hin jiru).

## 3. Production checklist
- [x] `MAVE_ENV=production` → secret laafaa/hin kaa'amne yoo ta'e app hin eegalu; demo data/answer hin deebi'u; `/docs` cufame; HSTS ni kaa'ama
- [x] Crawler SSRF ittisa (`app/core/netguard.py`): localhost, IP dhuunfaa (10.x, 192.168.x, 169.254.169.254 ...), DNS rebinding, redirect hundi ni qoratama
- [x] Rate limit: `/auth/*` irratti ciccimoo (`MAVE_AUTH_RATE_LIMIT`), security headers, CORS (`MAVE_CORS_ORIGINS`)
- [x] Rate limit hedduu worker irratti (v0.10.2): `MAVE_REDIS_URL` yoo kaa'ame, ulaagaan Redis keessatti walii gala (sliding-window counter; furtuun SHA-256 malee IP hin kaa'amu). Redis yoo hin argamne ni **fail-open** gara ulaagaa process tokkootti (API hin jiraatu); production keessatti `MAVE_REDIS_URL` malee warning ni baha. `docker-compose.prod.yml` Redis ofumaan dabala.
- [x] Cache walii gala (v0.10.3): Brave bu'aa fi deebii AI `MAVE_REDIS_URL` waliin worker hundaa gidduutti ni qoodama (JSON, furtuun hash, Redis cabe → process qofa). Deebii AI fi bu'aan barbaacha namni gaafate irratti hin hundoofne, kanaaf qooduun nageenya qaba.
- [ ] Gradle Wrapper guutuu (v0.10.6: **hin xumuramne — gradle-wrapper.jar binary dha, network barbaada**). Karaa 1: GitHub → Actions → "Generate Gradle Wrapper" → Run workflow → artifact `gradle-wrapper` buufadhu → `android/` keessatti unzip godhi → `git add android/gradlew android/gradlew.bat android/gradle/wrapper && git update-index --chmod=+x android/gradlew` → commit. Karaa 2 (kompiitara network qabu): `cd android && gradle wrapper --gradle-version 8.9`. Booda CI (`android` job) `./gradlew` ofumaan fayyadama.
- [ ] `backend/.env` keessatti key dhugaa (XAI_API_KEY) zip/git keessatti hin kennin; yoo kennite ykn ergite haaraa godhi (rotate)
- [ ] `MAVE_DATABASE_URL` + `POSTGRES_PASSWORD` jabaa kaa'i (production keessatti `MAVE_DATABASE_URL` malee app hin eegalu); `MAVE_JWT_SECRET`, `MAVE_ADMIN_KEY` jabaa kaa'i; HTTPS (Caddy/Nginx/Cloud Run) duuba; `MAVE_TRUST_PROXY=1`
- [x] SQLite → PostgreSQL (full-text, pool, migration, backup script)
- [ ] OpenSearch yoo index >1M page ta'e
- [ ] Release signing key, Play Console, privacy policy
- [ ] `MAVE_BOT_URL` = fuula crawler keessan ibsu (User-Agent keessatti ni ergama)

## 4. Master Plan v1.0 — haala amma (v0.10)
| Phase | Haala |
|---|---|
| 1 Stabilize | **Xumurame (backend)**: test admin-stats sirraa'e, URL fakkeenyaa haqame, API v1 prefix |
| 4 Crawler | **Xumurame (backend, v0.7)**: SSRF, sitemap (+index/gz), robots Crawl-delay, canonical, redirect, duplicate (exact + SimHash), retry/backoff, priority frontier, recrawl scheduler, DB migration v3. Hin eegalamne: JavaScript rendering (optional) |
| 5 AI | **Backend xumurame**: provider abstraction (xAI/Anthropic), citations hunda isaanii mirkanaa'an, "ragaa gahaa hin jiru" kallattiin, prompt-injection eegumsa (maddoota, prompt, deebii). Hin eegalamne: semantic retrieval (Phase 3), Android irratti warning "ungrounded" |
| 6 Users | **Backend xumurame**: refresh token (rotation + replay detection), logout/logout-all, forgot/reset password, email verification, preferences, account deletion. **Android**: refresh otomaatikii, logout server irratti, forgot/reset dialog. Hin eegalamne: verification UI, delete-account UI, preferences sync |
| 8 Web | **Xumurame (v0.8)**: responsive Web app (search, AI+citations, categories, auth, saved, history, settings, offline). Jijjiirama: plan **React/Next.js** jedha; amma **JS qulqulluu (build hin qabu)** — network malee mirkaneessuuf; booda React tti ceesisuun ni danda'ama |
| 9 Admin | **Xumurame (v0.8)**: dashboard (health, stats, seeds, crawler, errors, analytics, users, audit log). User disable (v0.11.0) fi user-level role (v0.11.4) xumurame. Hafe: dashboard email/password sign-in (amma key), alerts |
| 10 Release | **Muraasni (v0.8)**: CI keessatti Node test, Prometheus metrics, JSON logs, Caddy/prod compose template. v0.10.4: alert rules, restore --verify, load test. Hin eegalamne: staging, security scan blocking, uptime monitor, backup offsite |
| 2 Production data | **PostgreSQL xumurame (v1.0 jalqaba)**: pool, `schema_migrations` + advisory lock, full-text GIN, vocabulary materialized view, `FOR UPDATE SKIP LOCKED` frontier, `migrate_sqlite.py`, backup script. Redis (rate limit v0.10.2 + cache v0.10.3) xumurame; hafe: Alembic (amma `MIGRATIONS` dict), PITR backup |
| 7 Android | **Muraasni (v0.9)**: pagination/infinite scroll, AI citations + warning "ungrounded"/"insufficient". Hin eegalamne: module refactor, offline sync guutuu, verification/delete-account UI, preferences sync, Gradle Wrapper |
| 3 Search core | **Muraasni (v0.10)**: Afaan Oromoo stemmer + stem-prefix query (`app/search/oromoo.py`), `app/search/scoring.py`, `app/search/semantic.py` (Embedder + hashing embedder, default cufaa), eval harness (`python eval_search.py`, nDCG/MRR/Recall). Hin eegalamne: OpenSearch, pgvector/k-NN, embedding hiika beeku, dataset dhugaa. Ilaali `PLAN_STATUS.md` |
| 2 Redis/OpenSearch | Redis, OpenSearch — itti aanee. Ilaali `PLAN_STATUS.md` |

### Search core (v0.10) — Afaan Oromoo + qorannoo qulqullina

- **Query analysis (v0.10.5)**: `app/search/query.py::analyze(q, lang_hint)` — `lang` (`om`/`en`/`None`) fi `intent` (`news|images|videos|tech`/`None`, akeekkachiisa qofa). `GET /api/v1/search` → `{results, corrected, page, intent, query_lang}`; `intent` fuula 1 irratti `type=web` qofaaf. Web app "Kutaa kana ilaali" link ni agarsiisa.

- **Stemming**: gaaffiin Afaan Oromoo (jecha dhuunfaa `fi`, `kan`, `akka` ... qabaate, ykn `query_lang="om"` ergame) jecha hundaaf *stem* prefix ni barbaada: `barataa` → `barattoota`, `bulaa` → `bultoota`, `mana` → `manatti`/`manicha`. Seera salphaa (suffix) irratti hundaa'a; **ogeessa afaanii irraa mirkaneeffamuu qaba**. Gaaffii Ingliffaa hin jijjiiru.
- **Qorannoo**: `cd backend && python eval_search.py [--lang om] [--verbose]` — qindaa'inoota (baseline, +stem, +semantic) nDCG@10 / MRR / Recall@10 tiin wal bira qaba (DB hin barbaachisu). `eval/seed_dataset.json` **seed fakkeenya** qofa (docs/labels synthetic); dataset dhugaa fi labeling ogeeyyii Afaan Oromoo waliin bakka buusi, achiin dura ulfaatina (weights) hin jijjiiramin.
- **Semantic**: `MAVE_SEMANTIC_WEIGHT` (default `0` = cufaa). `HashingEmbedder` xiinxala *qubee/sub-word* ti (`postgres` ~ `postgresql`); **hiika (meaning) hin beeku** (`car` ≠ `automobile`). Model dhugaa `Embedder` interface tiin ni qabsiifama.

### Auth API (v0.4)
`POST /api/v1/auth/register|login` → `{token, refresh_token, expires_in, email, email_verified}` (`token` = daqiiqaa 15)
`POST /auth/refresh {refresh_token}` · `POST /auth/logout {refresh_token?, all?}` · `POST /auth/forgot-password {email}` · `POST /auth/reset-password {token, password}` · `POST /auth/verify-email {token}` · `POST /auth/resend-verification`
`GET /me` · `GET|PUT /me/preferences {lang, theme}` · `POST /me/delete {password}` (POST, sababiin isaa DELETE body proxy tokko tokko irratti ni haqama)

- Refresh token yeroo tokko qofa fayyada; ammas fayyadamuun yeroo duraa deebi'ee dhufe **session guutuu ni cufa** (hanga 90 guyyaa).
- Logout, password reset, account deletion booda access token **battalumatti** hojii dhaabu (session DB irratti ilaalama).
- Email: `MAVE_SMTP_*` kenni. Development keessatti email log irratti ni mul'ata; production keessatti SMTP malee email hin ergamu.
- Database: migration versioned (`app/db/migrations.py: MIGRATIONS`) tabula `schema_migrations` keessatti galmaa'a; `init_db()` yeroo jalqabaa ofumaan ni hojjata (worker hedduu irratti walitti hin bu'u — advisory lock).
