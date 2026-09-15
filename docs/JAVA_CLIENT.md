# Client Java per l'API Rizzo PII

Guida **per Claude Code** per implementare, in un'applicazione Java, il client dell'API di
anonimizzazione esposta dal container Rizzo PII (deploy: [DOCKER.md](DOCKER.md)).
Prima il **contratto** (verificato sul codice: `src/app/app.py`, `src/app/api_mode.py`), poi il
**design** del client, il **codice di riferimento**, i **test** e una **checklist** finale.

Il flusso che il client deve rendere semplice e sicuro:

```
testo/PDF ──POST /analyze──▶ anonymized_text + mapping ──▶ LLM esterno ──▶ risposta con [FULLNAME_1]…
                                    │ (resta in memoria, lato client)            │
                                    └───────────────── restore() ◀───────────────┘
```

---

## Regole per Claude Code

- **Prima di scrivere codice, leggere il progetto Java di destinazione**: versione di Java, build
  (Maven/Gradle), framework (Spring Boot? Quarkus? plain), libreria JSON già presente, stile di
  logging, come gestisce la configurazione e i segreti. **Adeguarsi** a quello che c'è: niente
  seconda libreria JSON, niente secondo client HTTP se ne esiste già uno.
- **La chiave API non va mai nel codice né in file versionati**: si legge da variabile d'ambiente /
  secret / config esterna. Nei test si usa una chiave finta.
- **Mai loggare** il testo inviato, `anonymized_text`, `mapping`, `segments`, `source_text`, né il
  corpo delle risposte: contengono dati personali o la chiave per ricostruirli. Loggare solo
  metodo, path, status, durata, `n_entities`.
- Il **`mapping` è materiale sensibile quanto il documento**: tenerlo in memoria per la durata del
  ciclo anonimizza → LLM → ripristino; non persisterlo, non metterlo in cache condivise, non
  serializzarlo in eccezioni o `toString()`.
- **Fail-closed**: se l'anonimizzazione fallisce (errore, timeout, 5xx) il testo **non** deve
  proseguire in chiaro verso l'LLM. Il client lancia un'eccezione; nessun fallback "manda l'originale".
- Non modificare il server da questa guida: se serve un endpoint nuovo, segnalarlo.

---

## 1. Contratto dell'API

Base URL configurabile (es. `https://pii.interno.example` dietro reverse proxy, oppure
`http://host:5005`). Tutte le risposte JSON sono UTF-8.

### Autenticazione

Su **tutti** gli endpoint tranne `/health` e `/healthz`, uno dei due header:

```
Authorization: Bearer <chiave>
X-API-Key: <chiave>
```

Chiave assente o errata → `401` con header `WWW-Authenticate: Bearer realm="rizzo-pii"`.

### `GET /health` — readiness, senza chiave, senza inferenza

```json
{"status":"ok","model_loaded":true,"model":"rizzo-pii-0.3B-v1.5.0","model_version":"1.5.0",
 "app_version":"2.0.0","device":"cpu","tags":23,"excluded_tags":[],"mapping_enabled":true,
 "api_mode":true,"auth_required":true}
```

`200` = pronto, `503` = modello non caricato. **Nota**: sotto gunicorn, durante il caricamento
iniziale del modello il server di solito **non risponde affatto** (connessione rifiutata o
timeout), non con 503. Il client deve trattare "non raggiungibile" e "503" allo stesso modo: non pronto.

### `POST /analyze` — anonimizza

**Input JSON** (`Content-Type: application/json`):

| Campo | Tipo | Obbl. | Note |
|---|---|---|---|
| `text` | string | sì | vuoto o solo spazi → `400` |
| `exclude_tags` | array di string **o** string CSV | no | tag rilevati ma **lasciati in chiaro** (es. `["AMOUNT","AGE"]`). Tag sconosciuti: ignorati senza errore |
| `include_mapping` | boolean (accetta anche `"true"/"false"/"1"/"0"`) | no | `false` = anonimizzazione **definitiva**. Default = impostazione del server (`PII_MAPPING`, di solito `true`) |

Se un campo opzionale **non** viene inviato vale il default del server. Il client deve inviare
**sempre esplicitamente** `include_mapping` (e `exclude_tags` se rilevante) per non dipendere dalla
configurazione del container.

**Input multipart** (`multipart/form-data`) per file: parte file con nome **`file`** (alias storico
`pdf`), estensioni `.pdf`, `.md`, `.markdown`, `.txt`, `.text` (senza estensione = testo). Campi
testuali opzionali `exclude_tags` (CSV) e `include_mapping` (`true`/`false`). Il testo del PDF viene
estratto dal layer testuale: una **scansione** non ha testo → `400` "Nessun testo".

**Output `200`**:

```json
{
  "anonymized_text": "Mi chiamo [FULLNAME_1], CF [CF_1], IBAN [IBAN_1]",
  "mapping": {"[FULLNAME_1]": "Mario Rossi", "[CF_1]": "RSSMRA85M01H501Z", "[IBAN_1]": "IT60X0542811101000000123456"},
  "mapping_enabled": true,
  "segments": [
    {"t": "Mi chiamo "},
    {"label": "FULLNAME", "ph": "[FULLNAME_1]", "src": "modello", "validated": false, "t": "Mario Rossi"},
    {"t": ", CF "},
    {"label": "CF", "ph": "[CF_1]", "src": "regex", "validated": false, "t": "RSSMRA85M01H501Z"}
  ],
  "n_entities": 3, "n_unique": 3, "n_chunks": 1, "n_chars": 67,
  "by_label": {"FULLNAME": 1, "CF": 1, "IBAN": 1},
  "by_source": {"modello": 1, "regex": 2},
  "excluded_tags": [],
  "source_text": "Mi chiamo Mario Rossi, CF RSSMRA85M01H501Z, IBAN IT60X0542811101000000123456"
}
```

Semantica da rispettare nel client:

- **Placeholder** = `[` + `LABEL` + `_` + `N` + `]`, `LABEL` in `[A-Z_]+`, `N` ≥ 1. Stesso valore
  (normalizzato) → stesso placeholder in tutto il documento.
- **`mapping` è sempre presente**: con `include_mapping=false` è **`{}`** (non assente) e i segmenti
  entità **non hanno il campo `t`**. Un segmento è "entità" se ha `label`; "testo" se ha solo `t`.
- `src` ∈ `"modello"`, `"regex"`; `validated=true` = checksum verificato (IBAN/CF/PIVA/carta).
  Un valore trovato dalla regex per **formato** ma con checksum errato ha `src:"regex"`,
  `validated:false` e viene **comunque anonimizzato** (è il caso del CF d'esempio qui sopra; l'IBAN
  invece è valido e ha `validated:true`). Non usare `validated` per decidere se anonimizzare.
- `source_text` = il testo inviato (per un file: quello estratto). Ignorarlo se non serve; non loggarlo.
- `by_label`, `by_source` sono mappe con chiavi variabili: deserializzarle come `Map<String,Integer>`.
- **Ignorare i campi sconosciuti** (il server può aggiungerne).

### `POST /pdf` — PDF anonimizzato

Stesso input di `/analyze` (`include_mapping` ignorato: la mappa resta nel server). Output
`200 application/pdf` (binario) con header:

| Header | Significato |
|---|---|
| `Content-Disposition` | `attachment; filename=<nome>_anonimizzato.pdf` |
| `X-PII-Redactions` | numero di redazioni/entità applicate |
| `X-PII-Residual` | valori **ancora leggibili** nell'output → **il PDF NON è sicuro** se > 0 |
| `X-PII-Skipped` | valori troppo corti per essere cercati (es. "45") rimasti in chiaro → avvisare |
| `X-PII-Notfound` | valori non ritrovati nel layout |

`422` = nessuna PII trovata, oppure PDF scansione (nessuna occorrenza nel layer testuale).
Il client deve **esporre** `residual`/`skipped` al chiamante, non scartarli.

### `GET /settings` — sola lettura in modalità API

Legenda dei tag e default del servizio: `{"tags":[{"tag","it","en","example"}…], "excluded_tags",
"mapping_enabled", "env_override", "read_only"}`. `POST /settings` → `403`. Utile per validare
`exclude_tags` lato client e costruire UI.

I **23 tag**: `FULLNAME AGE GENDER DATE TIME STREET BUILDINGNUM ZIPCODE CITY PROVINCE EMAIL
TELEPHONENUM CF PIVA ID_DOC IBAN CREDITCARDNUMBER AMOUNT TARGA ORG DOCID CATASTO URL`
(la fonte di verità è `GET /settings`, non questa lista).

### Errori

Corpo JSON `{"error": "<messaggio>"}`; gli errori generati dal livello API aggiungono `"status"`.
**Basarsi sullo status HTTP, non sul campo `status`** (non sempre presente). Il messaggio è in
italiano e **non** va mostrato tale e quale all'utente finale né usato per logica.

| Status | Quando | Retry? |
|---|---|---|
| `400` | testo vuoto, formato file non supportato, PDF corrotto/protetto | no |
| `401` | chiave mancante/errata | no (errore di configurazione) |
| `403` | `POST /settings` in modalità API | no |
| `404` | path errato | no |
| `405` | metodo errato (header `Allow`) | no |
| `413` | richiesta oltre `PII_MAX_UPLOAD_MB` (default 50 MB) | no |
| `422` | `/pdf`: nessuna PII / scansione | no |
| `500` | errore interno (dettagli solo nei log del server) | al massimo 1, poi errore |
| `502/503/504`, connessione rifiutata, timeout di connessione | proxy/container non pronto o riavvio | sì, con backoff |

### Prestazioni e concorrenza

- L'inferenza è **serializzata sul server** (una richiesta alla volta); richieste parallele fanno
  **coda**. Su CPU: frazioni di secondo per un paragrafo, **minuti** per un documento lungo
  (gunicorn ha timeout 600 s).
- Client: **timeout di connessione breve** (5-10 s), **timeout di richiesta lungo** e configurabile
  (default 300 s, 600 s per `/pdf`), **limite di concorrenza** lato client (default 2-4) per non
  accumulare richieste che scadrebbero in coda.
- Documenti enormi: il limite è sulla dimensione della richiesta, non sulle parole; il server fa
  chunking da solo. Non spezzare il testo lato client (romperebbe la numerazione coerente dei placeholder).

---

## 2. Design del client

### API pubblica (indipendente dal framework)

```java
public interface PiiAnonymizer {
    AnonymizeResult anonymize(String text, AnonymizeOptions options);      // POST /analyze JSON
    AnonymizeResult anonymizeFile(Path file, AnonymizeOptions options);    // POST /analyze multipart
    PdfResult anonymizePdf(Path file, AnonymizeOptions options);           // POST /pdf
    boolean isReady();                                                     // GET /health, mai eccezioni
    String restore(String textWithPlaceholders, Map<String, String> mapping);  // puro, nessuna chiamata HTTP
}
```

- `AnonymizeOptions`: `includeMapping` (default **true**, sempre inviato), `excludeTags` (`Set<String>`, default vuoto).
- `AnonymizeResult`: `anonymizedText`, `mapping` (`Map` immodificabile, mai null), `entities`
  (lista di `Entity{label, placeholder, source, validated}` ricavata dai segmenti), `nEntities`,
  `byLabel`. **`toString()` senza testo né mapping.**
- `PdfResult`: `byte[] content` (o stream), `filename`, `redactions`, `residual`, `skipped`,
  `notFound`, e `boolean isFullyRedacted()` = `residual == 0 && skipped == 0`.

### Eccezioni

```
PiiClientException (unchecked, base)
├── PiiConfigurationException     401, 403, 404, 405 → configurazione sbagliata, non ritentare
├── PiiInvalidInputException      400, 413, 422      → input del chiamante
├── PiiServiceUnavailableException 502/503/504, connessione, timeout di connessione → ritentabile
└── PiiServerException            500, risposta non interpretabile
```

Ogni eccezione porta `statusCode` e il messaggio del server; **mai** il testo inviato.

### Configurazione

| Proprietà | Default | Note |
|---|---|---|
| `baseUrl` | — | obbligatoria, senza `/` finale |
| `apiKey` | — | obbligatoria; da env `RIZZO_PII_API_KEY` o secret manager |
| `connectTimeout` | 10 s | |
| `requestTimeout` | 300 s | |
| `pdfTimeout` | 600 s | |
| `maxConcurrency` | 2 | `Semaphore` attorno alle chiamate che fanno inferenza |
| `retry.maxAttempts` | 3 | solo sui casi "ritentabili" |
| `retry.backoff` | 1 s → ×2, max 15 s | |

Validare all'avvio: `baseUrl` presente e `https` salvo host locale/rete interna esplicitamente
consentita; `apiKey` presente e ≥ 16 caratteri.

### Ripristino dei placeholder (`restore`)

Lavoro **puro lato client**. Gli LLM alterano i placeholder (tolgono le parentesi, aggiungono spazi o
grassetto markdown), quindi il match è tollerante ma **non deve** scambiare `[FULLNAME_1]` con
`[FULLNAME_12]`:

- per ogni chiave del `mapping`, `inner = LABEL_N` (senza parentesi);
- regex: `\*{0,2}(?:\[\s*)?` + `(?<![A-Z0-9_])` + `Pattern.quote(inner)` + `(?!\d)(?:\s*\])?\*{0,2}`.
  Gli spazi interni si accettano **solo dentro le parentesi**: con `\[?\s*` un placeholder senza
  parentesi si mangerebbe lo spazio che lo precede (`Sig. FULLNAME_1.` → `Sig.Mario Rossi.`).
  `(?!\d)` impedisce di trovare `FULLNAME_1` dentro `FULLNAME_12`; il lookbehind impedisce `XORG_1`;
- sostituzione con `Matcher.quoteReplacement(valore)` (i valori possono contenere `$` e `\`);
- ordinare le chiavi per lunghezza decrescente;
- restituire anche i placeholder **rimasti** (presenti nel testo ma non nel mapping) se il chiamante
  lo chiede: segnalano un LLM che ha inventato placeholder o un mapping sbagliato.

---

## 3. Codice di riferimento — Java 17, `java.net.http` + Jackson

Nessuna dipendenza oltre a Jackson (`com.fasterxml.jackson.core:jackson-databind`). Se il progetto
usa Gson o Spring, adattare solo la parte JSON/HTTP (vedi §4). Per Java 11 sostituire i `record`
con classi final.

```java
package it.example.pii;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.net.ConnectException;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.Semaphore;

public final class RizzoPiiClient implements PiiAnonymizer {

    public record Config(URI baseUrl, String apiKey, Duration connectTimeout, Duration requestTimeout,
                         Duration pdfTimeout, int maxConcurrency, int maxAttempts) {
        public Config {
            Objects.requireNonNull(baseUrl, "baseUrl");
            if (apiKey == null || apiKey.length() < 16) throw new IllegalArgumentException("apiKey mancante o < 16 caratteri");
        }
        public static Config of(String baseUrl, String apiKey) {
            return new Config(URI.create(baseUrl.replaceAll("/+$", "")), apiKey,
                    Duration.ofSeconds(10), Duration.ofSeconds(300), Duration.ofSeconds(600), 2, 3);
        }
        @Override public String toString() { return "Config[baseUrl=" + baseUrl + ", apiKey=***]"; }
    }

    private static final ObjectMapper JSON = new ObjectMapper()
            .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);

    private final Config cfg;
    private final HttpClient http;
    private final Semaphore permits;

    public RizzoPiiClient(Config cfg) {
        this.cfg = cfg;
        this.http = HttpClient.newBuilder().connectTimeout(cfg.connectTimeout()).build();
        this.permits = new Semaphore(cfg.maxConcurrency(), true);
    }

    // ------------------------------------------------------------------ API

    @Override
    public AnonymizeResult anonymize(String text, AnonymizeOptions opt) {
        if (text == null || text.isBlank()) throw new PiiInvalidInputException(400, "testo vuoto");
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("text", text);
        body.put("include_mapping", opt.includeMapping());
        body.put("exclude_tags", List.copyOf(opt.excludeTags()));
        HttpRequest req = base("/analyze", cfg.requestTimeout())
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofByteArray(write(body))).build();
        return toResult(send(req, HttpResponse.BodyHandlers.ofByteArray(), true).body());
    }

    @Override
    public AnonymizeResult anonymizeFile(Path file, AnonymizeOptions opt) {
        Multipart mp = new Multipart()
                .field("include_mapping", String.valueOf(opt.includeMapping()))
                .field("exclude_tags", String.join(",", opt.excludeTags()))
                .file("file", file);
        HttpRequest req = base("/analyze", cfg.requestTimeout())
                .header("Content-Type", mp.contentType()).POST(mp.publisher()).build();
        return toResult(send(req, HttpResponse.BodyHandlers.ofByteArray(), true).body());
    }

    @Override
    public PdfResult anonymizePdf(Path file, AnonymizeOptions opt) {
        Multipart mp = new Multipart().field("exclude_tags", String.join(",", opt.excludeTags())).file("file", file);
        HttpRequest req = base("/pdf", cfg.pdfTimeout())
                .header("Content-Type", mp.contentType()).POST(mp.publisher()).build();
        HttpResponse<byte[]> r = send(req, HttpResponse.BodyHandlers.ofByteArray(), true);
        HttpHeaders h = r.headers();
        String cd = h.firstValue("Content-Disposition").orElse("");
        String name = cd.contains("filename=") ? cd.substring(cd.indexOf("filename=") + 9).replace("\"", "") : "documento_anonimizzato.pdf";
        return new PdfResult(r.body(), name, intHeader(h, "X-PII-Redactions"), intHeader(h, "X-PII-Residual"),
                intHeader(h, "X-PII-Skipped"), intHeader(h, "X-PII-Notfound"));
    }

    @Override
    public boolean isReady() {
        try {
            HttpRequest req = HttpRequest.newBuilder(cfg.baseUrl().resolve("/health"))
                    .timeout(Duration.ofSeconds(5)).GET().build();       // /health non vuole la chiave
            return http.send(req, HttpResponse.BodyHandlers.discarding()).statusCode() == 200;
        } catch (IOException e) {
            return false;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    @Override
    public String restore(String text, Map<String, String> mapping) {
        return PlaceholderRestorer.restore(text, mapping);
    }

    // ------------------------------------------------------------ trasporto

    private HttpRequest.Builder base(String path, Duration timeout) {
        return HttpRequest.newBuilder(cfg.baseUrl().resolve(path))
                .timeout(timeout)
                .header("Authorization", "Bearer " + cfg.apiKey())
                .header("Accept", "application/json, application/pdf");
    }

    private <T> HttpResponse<T> send(HttpRequest req, HttpResponse.BodyHandler<T> handler, boolean inference) {
        long backoffMs = 1000;
        for (int attempt = 1; ; attempt++) {
            try {
                if (inference) permits.acquire();
                HttpResponse<T> r;
                try {
                    r = http.send(req, handler);
                } finally {
                    if (inference) permits.release();
                }
                int s = r.statusCode();
                if (s >= 200 && s < 300) return r;
                RuntimeException ex = mapError(s, r.body());
                if (!(ex instanceof PiiServiceUnavailableException) || attempt >= cfg.maxAttempts()) throw ex;
            } catch (HttpConnectTimeoutException | ConnectException e) {
                if (attempt >= cfg.maxAttempts()) throw new PiiServiceUnavailableException(0, "servizio non raggiungibile", e);
            } catch (HttpTimeoutException e) {
                // timeout DI RICHIESTA: l'inferenza potrebbe essere ancora in corso -> non ritentare
                throw new PiiServiceUnavailableException(0, "timeout della richiesta", e);
            } catch (IOException e) {
                if (attempt >= cfg.maxAttempts()) throw new PiiServiceUnavailableException(0, "errore di rete", e);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new PiiClientException(0, "interrotto", e);
            }
            sleep(backoffMs);
            backoffMs = Math.min(backoffMs * 2, 15_000);
        }
    }

    private static RuntimeException mapError(int status, Object body) {
        String msg = "HTTP " + status;
        if (body instanceof byte[] b && b.length > 0) {
            try {
                JsonNode e = JSON.readTree(b).get("error");
                if (e != null) msg = e.asText();
            } catch (IOException ignored) { /* corpo non JSON (proxy): resta "HTTP <status>" */ }
        }
        return switch (status) {
            case 400, 413, 422 -> new PiiInvalidInputException(status, msg);
            case 401, 403, 404, 405 -> new PiiConfigurationException(status, msg);
            case 502, 503, 504 -> new PiiServiceUnavailableException(status, msg, null);
            default -> new PiiServerException(status, msg);
        };
    }

    // ----------------------------------------------------------- mapping JSON

    @JsonIgnoreProperties(ignoreUnknown = true)
    record AnalyzeResponse(@JsonProperty("anonymized_text") String anonymizedText,
                           Map<String, String> mapping,
                           @JsonProperty("mapping_enabled") boolean mappingEnabled,
                           List<Segment> segments,
                           @JsonProperty("n_entities") int nEntities,
                           @JsonProperty("by_label") Map<String, Integer> byLabel) {}

    @JsonIgnoreProperties(ignoreUnknown = true)
    record Segment(String t, String label, String ph, String src, Boolean validated) {}

    private static AnonymizeResult toResult(byte[] body) {
        try {
            AnalyzeResponse r = JSON.readValue(body, AnalyzeResponse.class);
            if (r.anonymizedText() == null) throw new PiiServerException(200, "risposta senza anonymized_text");
            List<Entity> entities = r.segments() == null ? List.of() : r.segments().stream()
                    .filter(s -> s.label() != null)
                    .map(s -> new Entity(s.label(), s.ph(), s.src(), Boolean.TRUE.equals(s.validated())))
                    .toList();
            return new AnonymizeResult(r.anonymizedText(),
                    r.mapping() == null ? Map.of() : Map.copyOf(r.mapping()),
                    entities, r.nEntities(), r.byLabel() == null ? Map.of() : Map.copyOf(r.byLabel()));
        } catch (IOException e) {
            throw new PiiServerException(200, "risposta non interpretabile");   // niente body nel messaggio
        }
    }

    private static byte[] write(Object o) {
        try { return JSON.writeValueAsBytes(o); } catch (IOException e) { throw new IllegalStateException(e); }
    }

    private static int intHeader(HttpHeaders h, String name) {
        return h.firstValue(name).map(v -> { try { return Integer.parseInt(v.trim()); } catch (NumberFormatException e) { return 0; } }).orElse(0);
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new PiiClientException(0, "interrotto", e); }
    }

    // ------------------------------------------------------------- multipart

    /** java.net.http non ha multipart: costruzione minima, sufficiente per un file + campi. */
    static final class Multipart {
        private final String boundary = "----rizzo" + UUID.randomUUID().toString().replace("-", "");
        private final List<byte[]> parts = new ArrayList<>();

        Multipart field(String name, String value) {
            parts.add(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + name + "\"\r\n\r\n"
                    + value + "\r\n").getBytes(StandardCharsets.UTF_8));
            return this;
        }

        Multipart file(String name, Path path) {
            try {
                String filename = path.getFileName().toString().replace("\"", "_");
                String ct = filename.toLowerCase(Locale.ROOT).endsWith(".pdf") ? "application/pdf" : "text/plain; charset=utf-8";
                parts.add(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + name + "\"; filename=\""
                        + filename + "\"\r\nContent-Type: " + ct + "\r\n\r\n").getBytes(StandardCharsets.UTF_8));
                parts.add(Files.readAllBytes(path));
                parts.add("\r\n".getBytes(StandardCharsets.UTF_8));
                return this;
            } catch (IOException e) {
                throw new PiiInvalidInputException(400, "file non leggibile: " + path.getFileName());
            }
        }

        String contentType() { return "multipart/form-data; boundary=" + boundary; }

        HttpRequest.BodyPublisher publisher() {
            List<byte[]> all = new ArrayList<>(parts);
            all.add(("--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8));
            return HttpRequest.BodyPublishers.ofByteArrays(all);
        }
    }
}
```

```java
package it.example.pii;

import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Rimette i valori al posto dei placeholder nella risposta dell'LLM. Nessuna chiamata di rete. */
public final class PlaceholderRestorer {
    private PlaceholderRestorer() {}

    private static final Pattern KEY = Pattern.compile("\\[([A-Z_]+_\\d+)]");
    /** Placeholder ancora presenti dopo il ripristino (anche senza parentesi). */
    public static final Pattern LEFTOVER = Pattern.compile("(?<![A-Z0-9_])\\[?[A-Z]+(?:_[A-Z]+)*_\\d+(?!\\d)]?");

    public static String restore(String text, Map<String, String> mapping) {
        if (text == null || mapping == null || mapping.isEmpty()) return text;
        List<String> keys = new ArrayList<>(mapping.keySet());
        keys.sort(Comparator.comparingInt(String::length).reversed());
        String out = text;
        for (String ph : keys) {
            Matcher k = KEY.matcher(ph);
            if (!k.matches()) continue;                         // chiave non nel formato atteso: ignorata
            // spazi interni solo DENTRO le parentesi: "\\[?\\s*" mangerebbe lo spazio prima di un placeholder nudo
            Pattern p = Pattern.compile("\\*{0,2}(?:\\[\\s*)?(?<![A-Z0-9_])" + Pattern.quote(k.group(1)) + "(?!\\d)(?:\\s*])?\\*{0,2}");
            out = p.matcher(out).replaceAll(Matcher.quoteReplacement(mapping.get(ph)));
        }
        return out;
    }
}
```

Tipi di supporto (stesso package): `AnonymizeOptions(boolean includeMapping, Set<String> excludeTags)`
con `static AnonymizeOptions defaults()` = `(true, Set.of())`; `Entity(String label, String placeholder,
String source, boolean validated)`; `AnonymizeResult(...)` e `PdfResult(...)` come record con
`toString()` **ridefinito** (solo contatori); le quattro eccezioni del §2 con costruttore
`(int statusCode, String message[, Throwable cause])`.

**Attenzione al `toString()` generato dei record**: stamperebbe `mapping` e testo in qualunque log
o stack trace. Ridefinirlo sempre in `AnonymizeResult` e `PdfResult`.

---

## 4. Integrazione con framework

### Spring Boot 3.2+

- Proprietà `rizzo-pii.base-url`, `rizzo-pii.api-key` (da env `RIZZO_PII_API_KEY`), timeout e
  concorrenza in un `@ConfigurationProperties` validato (`@Validated`, `@NotBlank`, `@Size(min=16)`).
- Bean `PiiAnonymizer` costruito da quelle proprietà. Se il progetto usa già **`RestClient`**
  (sincrono) o **`WebClient`** (reattivo), implementare `PiiAnonymizer` con quello invece di
  `java.net.http`, mantenendo: header di autenticazione, timeout per endpoint, mappatura errori,
  retry solo sui casi ritentabili (Spring Retry / Resilience4j se già presenti), semaforo.
  Multipart: `MultipartBodyBuilder` / `LinkedMultiValueMap` con `FileSystemResource`.
- **Health indicator**: `HealthIndicator` che chiama `isReady()` → `DOWN` se false. Metterlo nel
  gruppo *readiness* solo se l'app non può funzionare senza anonimizzatore.
- Disattivare il logging dei body di `RestClient`/`WebClient` (`logging.level.org.springframework.web.client=INFO`, niente interceptor che stampano il payload).

### Quarkus / MicroProfile Rest Client

Interfaccia `@RegisterRestClient` con `@ClientHeaderParam(name="Authorization", value="Bearer ${rizzo-pii.api-key}")`,
`ResponseExceptionMapper` per la tabella errori, `@Timeout`/`@Retry` di SmallRye Fault Tolerance
solo sulle eccezioni ritentabili. Il ripristino resta `PlaceholderRestorer`.

---

## 5. Test

### Unit — senza server

- `PlaceholderRestorer`:
  - `[FULLNAME_1]` e `[FULLNAME_12]` entrambi nel mapping → nessuna contaminazione;
  - **solo** `[FULLNAME_1]` nel mapping e testo con `[FULLNAME_12]` → `[FULLNAME_12]` **intatto**;
  - varianti LLM: `FULLNAME_1`, `[ FULLNAME_1 ]`, `**[FULLNAME_1]**` → sostituite;
  - placeholder senza parentesi dopo uno spazio: `Sig. FULLNAME_1.` → `Sig. Mario Rossi.` (lo spazio **resta**);
  - valori con `$` e `\` (es. `"€ 1.000$"`) → nessuna eccezione, valore letterale;
  - mapping vuoto → testo invariato;
  - `ORG_1` non deve matchare dentro `XORG_1`.
- Deserializzazione: JSON reale di `/analyze` (quello del §1) → campi corretti; stesso JSON con un
  campo sconosciuto in più → nessun errore; `include_mapping=false` (`mapping: {}`, segmenti senza `t`).
- `toString()` di `AnonymizeResult`/`PdfResult`/`Config` **non** contiene testo, valori, chiave.

### HTTP mockato — WireMock o OkHttp `MockWebServer`

- header `Authorization: Bearer …` presente su `/analyze`, **assente** su `/health`;
- corpo inviato contiene `include_mapping` esplicito;
- mappatura status → eccezione per 400, 401, 413, 422, 500, 503 (503 ritentato fino a
  `maxAttempts`, 401 **mai** ritentato);
- corpo di errore non JSON (pagina HTML di un proxy) → eccezione con "HTTP <status>", nessun crash;
- `/pdf`: header `X-PII-*` letti; `isFullyRedacted()` false se `X-PII-Residual: 1`;
- multipart: parte `file` con filename, campi `exclude_tags`/`include_mapping`.

### Integrazione — container reale (Testcontainers)

```java
@Container
static GenericContainer<?> pii = new GenericContainer<>("ghcr.io/atasc/rizzo-pii:2.0.0-api")
        .withEnv("PII_API_MODE", "1")
        .withEnv("PII_API_KEY", "integration-test-key-123456")
        .withExposedPorts(5005)
        .waitingFor(Wait.forHttp("/health").forStatusCode(200).withStartupTimeout(Duration.ofMinutes(3)));
```

Richiede `docker login ghcr.io` sulla macchina/CI (immagine privata). Test minimi:
`anonymize("Mi chiamo Mario Rossi, IBAN IT60X0542811101000000123456")` →
`anonymizedText` contiene `[FULLNAME_1]` e `[IBAN_1]`, non contiene `Mario Rossi`;
`restore(anonymizedText, mapping)` = testo originale; chiave sbagliata → `PiiConfigurationException`.
Taggare questi test (es. `@Tag("integration")`) ed escluderli dalla build veloce: l'immagine è ~2,7 GB.
**Non** asserire il riconoscimento di entità ambigue (dipende dal modello); IBAN/CF/email sono
stabili perché coperti dalla rete regex+checksum.

---

## 6. Checklist finale (Claude Code la verifica prima di dichiarare finito)

- [ ] Nessuna chiave nel codice/config versionata; letta da env/secret; `toString()` mascherato.
- [ ] Nessun log di testo, `anonymized_text`, `mapping`, `segments`, body di risposta.
- [ ] `include_mapping` sempre inviato esplicitamente.
- [ ] Errore/timeout di anonimizzazione → eccezione; nessun invio del testo originale all'LLM.
- [ ] Retry solo su 502/503/504 e problemi di connessione; mai su 4xx né su timeout di richiesta.
- [ ] Timeout di richiesta ≥ 300 s (600 s per `/pdf`) e limite di concorrenza lato client.
- [ ] `/pdf`: `residual`/`skipped` esposti al chiamante.
- [ ] `restore` non contamina `[X_1]` / `[X_12]` e gestisce `$` nei valori.
- [ ] Test unit + mock verdi; integrazione con Testcontainers eseguita almeno una volta.
- [ ] Nessuna dipendenza aggiunta se il progetto ne aveva già una equivalente.
