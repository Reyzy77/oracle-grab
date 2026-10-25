# oracle-grab — capter une VM Oracle Cloud « Always Free »

Script qui obtient une instance `VM.Standard.A1.Flex` (ARM, jusqu'à 4 OCPU / 24 GB gratuits à vie)
dès qu'Oracle libère de la capacité, sans se faire rate-limiter.

## Ce qui le distingue d'une simple boucle de retry

**Il utilise la sonde de capacité d'Oracle.** L'API `CreateComputeCapacityReport` dit si une shape
est disponible dans une availability domain **sans consommer le quota de requêtes `LaunchInstance`**.
Conséquence directe :

| | boucle classique | ce script |
|---|---|---|
| fréquence de vérification | 5 min (sinon 429) | **45–60 s** |
| délai entre capacité libérée et lancement | jusqu'à 5 min | **~1 s** |
| risque de rate limit | élevé | quasi nul (on ne lance que sur du sûr) |

La capacité ARM gratuite part en quelques secondes quand elle apparaît. C'est ce délai de réaction
qui fait la différence, pas le nombre de tentatives.

**Les autres optimisations :**

- **Tous les fault domains, pas juste les ADs.** La capacité apparaît souvent dans un seul FD ; la
  sonde indique lequel et le lancement le cible directement.
- **Échelle de configurations.** Si 4 OCPU/24 GB est plein, il tente 3/18, puis 2/12, puis 1/6 — bien
  plus faciles à obtenir. Tu agrandis la machine après coup dans la console (`TRY_SMALLER_SHAPES`).
- **Calcul du quota restant.** Il liste tes instances existantes et ne demande jamais plus que le
  quota gratuit restant, ce qui évite les rejets `LimitExceeded` inutiles.
- **Ordre des ADs aléatoire** à chaque cycle, pour ne pas toujours taper la même en premier.
- **Auto-détection** du subnet et de l'image (y compris la bonne image selon ARM ou AMD).
- **Idempotence** : si l'instance existe déjà, il sort immédiatement en succès.
- **Fallback AMD** optionnel (`FALLBACK_TO_MICRO`) : `E2.1.Micro` est presque toujours disponible.

## Gestion des erreurs

Quatre cas, traités différemment — c'est le cœur du problème :

| Erreur | Signification | Réaction |
|---|---|---|
| `500 Out of host capacity` | Oracle n'a plus de machine libre | AD/FD suivant, cycle suivant |
| `429 TooManyRequests` | **tu** tapes trop fort | backoff exponentiel + respect de `Retry-After` |
| `LimitExceeded` | quota free tier déjà consommé | **arrêt** (insister ne sert à rien) |
| `401/403` | clé API ou policy IAM cassée | **arrêt** |

Spammer toutes les 10 s ne donne pas la machine plus vite : ça ne fait que déclencher le 429, et
pendant le backoff tu ne vois plus rien passer. L'espacement minimum entre appels est garanti par
un pacer interne.

## Setup

### 1. Clé API OCI
Console Oracle → avatar en haut à droite → **My profile** → **API keys** → **Add API key** →
*Generate API key pair*, télécharge la clé privée. Oracle affiche ensuite un bloc de config :
récupères-y `user`, `fingerprint`, `tenancy`, `region`.

Si tu n'es pas administrateur du tenancy, il faut aussi une policy IAM :
```
Allow group <ton-groupe> to manage instance-family in tenancy
Allow group <ton-groupe> to use virtual-network-family in tenancy
Allow group <ton-groupe> to read app-catalog-listing in tenancy
```

### 2. Réseau
Il faut un VCN avec un **subnet public**. Si tu n'en as pas : Console → Networking → Virtual Cloud
Networks → *Start VCN Wizard* → « VCN with Internet Connectivity ».

### 3. Config et vérification
```bash
cp .env.example .env     # puis remplis-le
pip install -r requirements.txt

python oci_grab.py discover   # ADs, fault domains, subnets, images, quota restant
python oci_grab.py probe      # état de la capacité maintenant, sans rien créer
python oci_grab.py run --dry-run   # déroule tout sauf la création
```

### 4. Lancer
```bash
python oci_grab.py run          # boucle jusqu'à obtenir la machine
python oci_grab.py run --once   # un cycle borné (cron)
```

Codes de sortie : `0` obtenue · `1` pas de capacité · `2` max tentatives · `3` quota · `4` auth · `5` autre.

### Docker
```bash
docker build -t oracle-grab .
docker run -d --restart unless-stopped --env-file .env --name grab oracle-grab
```

### Notifications
`TELEGRAM_BOT_TOKEN` (via @BotFather) + `TELEGRAM_CHAT_ID`, ou `WEBHOOK_URL` : tu reçois un message
dès que la machine est créée ou que la boucle s'arrête sur une erreur.

---

## Où l'héberger gratuitement en 24/7

### 1. GitHub Actions — recommandé
`.github/workflows/grab.yml` est déjà prêt : cron toutes les 5 min, et chaque run **sonde en continu
pendant 5 minutes** (`MAX_RUNTIME_SECONDS=300`) avant de rendre la main au suivant. La couverture est
donc quasi permanente, sans serveur à maintenir.

- Gratuit et illimité sur un **repo public** ; 2 000 min/mois sur un repo privé (≈ 6 jours de
  surveillance continue, largement assez).
- Secrets dans **Settings → Secrets and variables → Actions**. Pour la clé privée :
  `base64 -w0 oci_api_key.pem` et colle le résultat dans `OCI_KEY_CONTENT` — le script décode seul.
- ⚠️ GitHub désactive les crons d'un repo **inactif depuis 60 jours**.
- ⚠️ Repo public : ne commit jamais ton `.env` (il est dans `.gitignore`).

### 2. Google Cloud — Always Free `e2-micro`
Une vraie VM gratuite à vie (`us-central1` / `us-west1` / `us-east1`, 1 vCPU partagé, 1 GB, 30 GB).
Tu y mets le conteneur avec `--restart unless-stopped` et tu oublies. Carte bancaire demandée à
l'inscription, aucun débit dans le quota. La meilleure option si tu veux un process permanent avec
des sondes toutes les 45 s sans interruption.

### 3. Oracle lui-même — `VM.Standard.E2.1.Micro`
Ironique mais efficace : les shapes AMD micro passent presque toujours du premier coup. Crée-en une à
la main, puis fais tourner le conteneur dessus pour aller chercher l'ARM.

### 4. Autres
- **Koyeb / Render / Railway** — tiers gratuits ou crédits d'essai, mais veille des services web
  gratuits et crédits qui s'épuisent. Utilisable, pas durable.
- **Fly.io** — plus de vrai free tier depuis 2024.
- **Un Raspberry Pi ou un vieux PC chez toi** — zéro compte, zéro limite. Si tu en as un, c'est
  objectivement le meilleur choix.

---

## Les deux leviers qui comptent vraiment

1. **Passer ton compte en pay-as-you-go.** L'upgrade est gratuit, tu gardes toutes les ressources
   Always Free, et les comptes payants sont **prioritaires sur la capacité** face aux comptes trial.
   C'est, de loin, ce qui change le plus les chances d'obtenir une ARM.
2. **Demander moins.** Une 1 OCPU / 6 GB se trouve en heures là où une 4/24 peut prendre des
   semaines. Tu peux soit l'agrandir ensuite, soit prendre 4 machines de 1 OCPU.

Si ta home region est saturée depuis des mois, tu peux aussi la changer (une fois par mois) :
Administration → Tenancy details → Manage home region.
