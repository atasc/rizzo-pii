# Deploy su Docker — API server (build, push su GHCR, Portainer)

Guida **operativa per Claude Code** (e per chi la segue a mano): portare Rizzo PII da questa repo
a un container che espone l'**API di anonimizzazione** su una macchina gestita con **Portainer**.
Ogni passo ha una **verifica**: non si passa al successivo finché la verifica non è verde.

Riferimenti: [`Dockerfile`](../Dockerfile) (immagine), [`src/app/api_mode.py`](../src/app/api_mode.py)
(modalità API), [`deploy/portainer-stack.yml`](../deploy/portainer-stack.yml) (stack),
[`docker-compose.yml`](../docker-compose.yml) (uso locale con build, solo localhost).

```
repo ──docker build──▶ ghcr.io/atasc/rizzo-pii:<tag> ──docker push──▶ GHCR (privato)
                                                                          │ pull (token read:packages)
                                                           Portainer stack ▼
                                                     container :5005  ──▶  app client (API key)
```

---

## Regole per Claude Code

- **Mai inserire token o password** (GitHub PAT, chiave API reale in chiaro nei comandi che restano
  nella cronologia). `docker login` lo fa l'utente nel suo terminale; dopo, Claude può usare le
  credenziali salvate (`docker push`, `docker pull`).
- **Mai stampare** il contenuto di `~/.docker/config.json` né la chiave `auth` che contiene: è il
  token codificato in base64, non cifrato.
- **Chiedere prima** di: push su un registry, cambiare visibilità del pacchetto, deploy/aggiornamento
  dello stack, `docker logout`. Build e test locali si fanno senza chiedere.
- **Tag versionati** (`2.0.0-api`, `2.0.1-api`…), **mai `latest`**: si deve sapere cosa gira e poter
  tornare indietro.
- I container di prova si chiamano `rizzo-pii-smoke*`, pubblicano **solo su `127.0.0.1`** e si
  **rimuovono** a fine test (`docker rm -f`).
- Operazioni lunghe (build ~10 min la prima volta, push ~2,7 GB) → **in background** con log su un
  file in una cartella che **esiste** (verificarla prima: un redirect verso una directory mancante
  fa uscire la shell con exit 1 *senza* eseguire il comando, e il task risulta "completato").

---

## 0. Prerequisiti e verifica dell'ambiente

```bash
docker version --format 'client {{.Client.Version}} / server {{.Server.Version}}'
docker context ls
docker info --format '{{.Architecture}} {{.NCPU}}cpu {{.MemTotal}}'
```

Atteso: client **e** server valorizzati, contesto attivo `default`, architettura `x86_64`
(i server sono quasi sempre amd64; su un PC ARM serve `--platform linux/amd64` + emulazione).

### Problemi noti (tutti già incontrati)

| Sintomo | Causa | Rimedio |
|---|---|---|
| `Dependency is not satisfiable: docker-ce-cli` installando il `.deb` di Docker Desktop | repository apt di Docker non configurato | aggiungere il repo (sotto) oppure, meglio, usare **Docker Engine** senza Desktop |
| il repo apt di Docker dà 404 su **Linux Mint** | la guida ufficiale usa `VERSION_CODENAME` (`zena`), che Docker non ha | usare **`UBUNTU_CODENAME`** (`noble`) |
| `failed to connect ... ~/.docker/desktop/docker.sock` | contesto `desktop-linux` rimasto da un tentativo con Docker Desktop | `docker context use default` |
| `permission denied ... /var/run/docker.sock` | utente aggiunto al gruppo `docker` ma sessione non rinnovata | l'utente fa logout/login o `newgrp docker`; **Claude usa `sg docker -c "…"`** |
| build: `error getting credentials - exec: "docker-credential-desktop"` | `"credsStore": "desktop"` rimasto in `~/.docker/config.json` | togliere **solo** quella chiave (se `auths` è vuoto non si perde nulla) |
| `WARNING! Your credentials are stored unencrypted` dopo il login | nessun credential helper | accettabile su workstation personale; `docker logout ghcr.io` a fine push |

Installazione Docker Engine su Ubuntu **e derivate (Mint)** — la esegue l'utente (`sudo`):

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER      # poi logout/login
```

---

## 1. Build dell'immagine

Dalla root della repo. Il contesto di build è minimo (il `.dockerignore` riammette solo `src/app/`):
la cartella `models/` locale **non** entra nell'immagine, il modello viene scaricato da Hugging Face
in fase di build (`MODEL_REPO`/`MODEL_REVISION` negli `ARG` del Dockerfile).

```bash
TAG=2.0.0-api
docker build --progress=plain --platform linux/amd64 -t ghcr.io/atasc/rizzo-pii:$TAG . > build.log 2>&1; echo "exit=$?"
```

**Verifica**

```bash
grep -E "ERROR|naming to" build.log | tail -3        # atteso: "naming to ghcr.io/atasc/rizzo-pii:<tag> done"
docker images ghcr.io/atasc/rizzo-pii                # atteso: ~2,7 GB content size, creata ora
```

Se il codice di `src/app/` è cambiato, controllare che l'immagine contenga davvero la modifica
(il tag può restare quello di una build precedente se la build è fallita):

```bash
docker run --rm --entrypoint grep ghcr.io/atasc/rizzo-pii:$TAG -c '<stringa nuova>' /app/src/app/app.py
```

---

## 2. Smoke test locale (obbligatorio prima del push)

Tre casi. Si possono lanciare in parallelo (porte e nomi diversi).

### 2a. Modalità API con chiave — deve funzionare

```bash
K=smoke-test-key-1234567890        # chiave FINTA, solo per il test
docker run -d --name rizzo-pii-smoke -p 127.0.0.1:5995:5005 \
  -e PII_API_MODE=1 -e PII_API_KEY=$K ghcr.io/atasc/rizzo-pii:$TAG
# attesa healthy (il modello carica in ~10-60 s)
for i in $(seq 150); do s=$(docker inspect -f '{{.State.Health.Status}} {{.State.Status}}' rizzo-pii-smoke)
  case "$s" in healthy*|*exited*) break;; esac; sleep 2; done; echo "$s"
U=http://127.0.0.1:5995
curl -s -o /dev/null -w '%{http_code}\n' $U/health                                          # 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST $U/analyze -H 'Content-Type: application/json' -d '{"text":"x"}'   # 401
curl -s -X POST $U/analyze -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"text":"Mi chiamo Mario Rossi, CF RSSMRA85M01H501Z, IBAN IT60X0542811101000000123456"}'   # [FULLNAME_1] [CF_1] [IBAN_1] + mapping
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $K" $U/                              # 404 (UI spenta)
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "X-API-Key: $K" -H 'Content-Type: application/json' -d '{}' $U/settings   # 403
for n in 1 2 3 4; do curl -s -o /dev/null -w '%{http_code} ' -X POST $U/analyze -H "X-API-Key: $K" \
  -H 'Content-Type: application/json' -d '{"text":"Luca Verdi, tel 333 1234567"}' & done; wait; echo   # 200 x4
docker logs rizzo-pii-smoke 2>&1 | grep -iE "traceback|borrowed|error" | grep -vi fitz            # vuoto
docker rm -f rizzo-pii-smoke
```

### 2b. Modalità API senza chiave — deve FERMARSI

```bash
docker run --name rizzo-pii-smoke-nokey -e PII_API_MODE=1 ghcr.io/atasc/rizzo-pii:$TAG > /dev/null 2>&1
docker inspect -f '{{.State.ExitCode}}' rizzo-pii-smoke-nokey      # atteso: 3, in pochi secondi
docker logs rizzo-pii-smoke-nokey 2>&1 | grep -E "ERRORE|Worker failed to boot"
docker rm rizzo-pii-smoke-nokey
```

**Perché 3**: sotto gunicorn `app.py` è importato da un *worker*; se esce con un codice qualsiasi il
master lo rilancia **all'infinito** (container "Up", unhealthy, mai una richiesta servita). Solo
`WORKER_BOOT_ERROR = 3` ferma il master. Se questo test **non** termina entro ~30 s, il fix in
`app.py` manca: fermarsi e segnalarlo, non fare il push.

### 2c. Senza variabili — comportamento storico (UI)

```bash
docker run -d --name rizzo-pii-smoke-ui -p 127.0.0.1:5996:5005 ghcr.io/atasc/rizzo-pii:$TAG
# attesa healthy come sopra, poi:
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://127.0.0.1:5996/          # 200 text/html
docker rm -f rizzo-pii-smoke-ui
```

---

## 3. Push su GitHub Container Registry

**Login — lo fa l'utente**, con un PAT *classic* con scope **`write:packages`**
(https://github.com/settings/tokens):

```bash
echo "<TOKEN>" | docker login ghcr.io -u atasc --password-stdin     # "Login Succeeded"
```

Push (Claude può lanciarlo dopo il login, in background):

```bash
docker push ghcr.io/atasc/rizzo-pii:$TAG > push.log 2>&1; echo "exit=$?"
```

**Verifica — sul registry, non in locale** (`RepoDigests` locale esiste anche senza push):

```bash
tr '\r' '\n' < push.log | grep -iE "digest:|denied|unauthorized"     # "<tag>: digest: sha256:…"
docker buildx imagetools inspect ghcr.io/atasc/rizzo-pii:$TAG        # Platform: linux/amd64
```

La voce `Platform: unknown/unknown` nel manifest è l'**attestazione** di build, non una seconda
immagine. Il pacchetto compare su **https://github.com/atasc?tab=packages** (profilo → Packages,
**non** dentro la repo) ed è **privato** di default. Opzionale: *Package settings → Connect repository*.

| Errore | Causa |
|---|---|
| `permission denied ... docker.sock` | gruppo docker non attivo nel terminale (vedi §0) — il login invece riesce lo stesso |
| `denied` / `permission_denied: write_package` | token senza `write:packages` |
| pagina Packages vuota | push mai partito o fallito: verificare con `imagetools inspect` |

A push concluso, se non servono altri push a breve: `docker logout ghcr.io` (chiedere all'utente).

---

## 4. Deploy con Portainer

Passi nella UI di Portainer — li esegue l'utente; Claude li guida e verifica da fuori.

1. **Token di sola lettura** su GitHub: PAT classic con **solo `read:packages`** (diverso da quello del push).
2. **Registries → Add registry → Custom registry**: URL `ghcr.io`, autenticazione on, utente `atasc`,
   password = token `read:packages`.
3. **Stacks → Add stack** → nome `rizzo-pii` → *Web editor*: incollare
   [`deploy/portainer-stack.yml`](../deploy/portainer-stack.yml)
   (oppure *Repository*: `https://github.com/atasc/rizzo-pii`, compose path `deploy/portainer-stack.yml`).
4. **Environment variables** dello stack:

   | Variabile | Obbligatoria | Valore |
   |---|---|---|
   | `PII_API_KEY` | **sì** | `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` — ≥ 16 caratteri; più chiavi separate da virgola |
   | `IMAGE_TAG` | no | default `2.0.0-api` |
   | `PII_PUBLISH_PORT` | no | default `5005` |

   La chiave va **nelle variabili dello stack, non nello YAML** (lo YAML può finire in git).
5. **Deploy the stack**. Il primo pull scarica ~2,7 GB.

**Verifica** (da un PC in rete con la macchina):

```bash
curl -s http://<ip-macchina>:5005/health          # 200, "api_mode":true, "auth_required":true
curl -s -X POST http://<ip-macchina>:5005/analyze -H "Authorization: Bearer <PII_API_KEY>" \
  -H 'Content-Type: application/json' -d '{"text":"Mario Rossi, IBAN IT60X0542811101000000123456"}'
```

In Portainer il container deve essere **healthy** e nei *Logs* deve comparire `Modello pronto.`

| Sintomo in Portainer | Causa |
|---|---|
| `pull access denied` / `unauthorized` | registry non aggiunto, token senza `read:packages`, o immagine non pushata |
| stato **Restarting**, log `ERRORE: PII_API_MODE=1 senza PII_API_KEY` | variabile `PII_API_KEY` mancante (il deploy da YAML si ferma prima, con `${PII_API_KEY:?}`) |
| log `ERRORE: ... almeno 16 caratteri` | chiave troppo corta |
| healthy ma `curl` da fuori non risponde | firewall sulla porta pubblicata |
| container ucciso durante un documento lungo | RAM: servono ~4 GB liberi (limite nello stack 6 G) |

---

## 5. Aggiornamento

1. Modifiche al codice → test (`python -m unittest discover tests`) → commit.
2. **Nuovo tag** (`2.0.1-api`) → §1 build → §2 smoke test → §3 push.
3. Portainer: *Stacks → rizzo-pii → Editor* → `IMAGE_TAG=2.0.1-api` → **Update the stack** con
   *Re-pull image*. Rollback = rimettere il tag precedente.

Il volume `rizzo-pii-home` (preferenze) sopravvive agli aggiornamenti.

---

## 6. Uso dell'API da un'app esterna

Tutte le richieste (tranne `/health`) con `Authorization: Bearer <chiave>` oppure `X-API-Key: <chiave>`.

| Endpoint | Input | Output |
|---|---|---|
| `GET /health` | — | `200` pronto / `503` modello in caricamento. Senza chiave |
| `POST /analyze` | JSON `{"text", "exclude_tags"?, "include_mapping"?}` oppure multipart `file` (`.pdf/.md/.txt`) | `anonymized_text`, `mapping` (se attivo), `segments`, `by_label`, `n_entities` |
| `POST /pdf` | come `/analyze` | PDF anonimizzato + header `X-PII-Redactions/Residual/Skipped` |
| `GET /settings` | — | legenda dei 23 tag, default del servizio (sola lettura) |

Errori sempre JSON `{"error", "status"}`: `400` input, `401` chiave, `403` settings, `404`, `405`,
`413` oltre `PII_MAX_UPLOAD_MB`, `422` nessuna PII / PDF scansione, `500` (senza dettagli).

Il flusso tipico: `POST /analyze` → invio di `anonymized_text` all'LLM → sostituzione dei
placeholder nella risposta con i valori di `mapping` **lato client** (il server non ha un endpoint
di ripristino e non conserva nulla). Per un'anonimizzazione definitiva: `"include_mapping": false`.

Note di esercizio: HTTP in chiaro → davanti un **reverse proxy TLS** (Traefik, Nginx Proxy Manager,
Caddy) oppure porta ristretta via firewall; inferenza **serializzata** (una richiesta alla volta,
un documento lungo su CPU sono minuti) → timeout alti lato client; CORS solo se si chiama da
browser (`PII_CORS_ORIGINS`).
