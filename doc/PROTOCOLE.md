# Protocole UDP de udg-throw-line-service

Pour les développeurs du jeu. Le jeu ne touche ni à la caméra ni au GPIO du
laser : il parle au service par datagrammes UDP sur **`127.0.0.1:47810`**
(adresse et port dans `/etc/udg-throw-line.conf`, section `[network]`).
L'autoload d'exemple [`godot/udg_throw_line.gd`](../godot/udg_throw_line.gd)
enveloppe tout ce qui suit.

## Principes

- **Un datagramme = une commande** texte ASCII. Pas de connexion.
- Mots-clés **insensibles à la casse**, arguments séparés par des espaces,
  paramètres en **`clé=valeur`** (une valeur ne contient jamais d'espace).
- **Réponse** : une ligne terminée par `\n`, envoyée à l'adresse source.
  Toute réponse **répète le mot-clé de la commande** :
  `ok <commande> [champs]` ou `err <commande> <message>` (sauf `ping` →
  `pong …`). `calibrate` répond environ une seconde plus tard : le client
  sait ainsi à quoi correspond chaque réponse même s'il a envoyé d'autres
  commandes entre-temps. Une commande illisible donne `err ? <message>`.
- Le service ne plante jamais et ne bloque jamais sur une commande invalide.
- **Événements** : poussés aux clients abonnés (`subscribe`), ils commencent
  toujours par `event` et ne se confondent pas avec une réponse.
- UDP : si le service est absent, rien ne se passe et rien ne bloque. Les
  réponses sont facultatives à lire.

## Commandes

| Commande | Effet | Réponse |
|---|---|---|
| `ping` | — | `pong <version> api=<n> laser=… camera=… calibrated=… watching=…` |
| `status` | — | `ok status <clé=valeur …>` (voir plus bas) |
| `laser on` / `laser off` | allume / éteint la ligne laser | `ok laser on`, ou `err laser output unavailable: …` |
| `subscribe` | abonne l'adresse source aux événements pour `ttl` s | `ok subscribe ttl=10`, puis l'état courant (voir plus bas) |
| `unsubscribe` | fin de l'abonnement | `ok unsubscribe` |
| `calibrate [line=x0,y0,x1,y1]` | cherche la ligne et l'enregistre (≈ 1 s, **personne sur la ligne**) | `ok calibrate line=… samples=… usable=… contrast=… orientation=… mode=…` ou `err calibrate <pourquoi>` |
| `forget` | oublie la calibration | `ok forget` |
| `snapshot` | écrit l'image courante en BMP | `ok snapshot path=/run/udg-throw-line/snapshot.bmp size=320x240` |
| `camera [exposure=<lignes>] [gain=<n>]` | lit / règle exposition et gain (non persistant) | `ok camera exposure=… gain=…` |
| `sim foot pos=<0-1> [width=<0-1>] [duration=<s>] [lift=<px>]`, `sim clear` | `--simulate` seulement : pose / retire un pied virtuel | `ok sim foot from=… to=… duration=…` |

Détails :

- **`laser`** : la ligne n'est surveillée que laser allumé. Le service
  retient l'état demandé : si la sortie est absente (pas d'entrée
  `udg-laser` dans le device tree, GPIO occupé), il répond `err` puis
  réessaie toutes les 5 s. Pendant une calibration, la réponse est
  `ok laser on deferred=calibrating` : l'état demandé s'applique à la fin.
- **`calibrate`** : le service éteint le laser, prend quelques images,
  l'allume, en reprend quelques-unes, trouve la ligne et mesure sa
  référence, puis remet le laser dans l'état demandé par le jeu. La
  calibration est gardée dans `/data/udg-throw-line/calibration.conf` et
  rechargée au démarrage ; elle n'est à refaire que si la caméra ou le laser
  bouge. `line=` impose la ligne (coordonnées normalisées 0–1 dans l'image)
  quand la recherche automatique échoue. ` warning=not-saved` en fin de
  réponse : calibration active mais pas écrite sur `/data`.
- **`snapshot`** : image couleur à demi-résolution de l'image brute, avec
  les échantillons de la ligne dessinés de part et d'autre du laser (vert =
  intact, jaune = coupé dans la dernière image, magenta = non surveillé,
  cyan = surveillance arrêtée). Godot la lit avec
  `Image.load_bmp_from_buffer()` ; utile pour un écran de réglage.

### `status`

```
ok status laser=on laser_wanted=on output=led:udg-laser camera=streaming source=v4l2:/dev/video11,GB10,640x480,GBRG,raw16 fps=89.9 frames=12345 exposure=300 gain=16 calibrated=yes line=0.0953,0.6187,0.9047,0.5513 samples=130 usable=128 watching=yes state=clear coverage=0.00 crossings=3 subscribers=1
```

- `laser` : état réel de la sortie ; `laser_wanted` : état demandé ;
  `output` : `led:<nom>`, `gpio:<puce>:<ligne>`, `none`, suffixe
  `(unavailable)` si la sortie manque ;
- `camera` : `streaming`, `absent` (pas de nœud), `error`, `stopped` ;
- `watching` : la ligne est-elle surveillée (laser allumé, calibrée,
  caméra en marche, pas de calibration en cours) ;
- `state` : état de la ligne (voir plus bas) ; `coverage` : part de la ligne
  coupée dans la dernière image ; `crossings` : franchissements depuis le
  démarrage.

## Événements

Le jeu envoie `subscribe` au démarrage puis **toutes les 3 s environ**
(l'abonnement dure `subscription_ttl` = 10 s) : si le service redémarre, le
jeu est de nouveau abonné quelques secondes plus tard ; si le jeu plante,
l'abonnement s'éteint seul. Un renouvellement ne renvoie que
`ok subscribe ttl=10`. Un **nouvel** abonné reçoit aussitôt l'état courant
(`event service …` puis `event line …`) : quelqu'un est peut-être déjà sur
la ligne.

### `event line`

Changement d'état de la ligne. `seq` augmente à chaque événement.

| Événement | Sens |
|---|---|
| `event line state=clear seq=7` | ligne entière (premier état après le démarrage de la surveillance) |
| `event line state=crossed seq=8 pos=0.420 from=0.380 to=0.460 runs=1` | **un pied franchit la ligne** |
| `event line state=clear seq=9 duration=0.84` | ligne de nouveau entière ; `duration` = durée du franchissement en s |
| `event line state=blocked seq=10 coverage=0.82` | plus de `max_width` (60 %) de la ligne coupée : caméra masquée, laser en défaut, quelqu'un en travers de toute la ligne — on ne peut plus juger |
| `event line state=unknown seq=11 reason=laser-off` | surveillance arrêtée ; `reason` : `laser-off`, `not-calibrated`, `camera`, `calibrating`, `starting` |

- `pos`, `from`, `to` : position le long de la ligne calibrée, de 0 à 1 ;
  0 est l'extrémité gauche dans l'image (haute pour une ligne plutôt
  verticale), `reverse_position = yes` inverse le sens. `pos` est le milieu
  du plus long tronçon coupé, `runs` le nombre de tronçons (deux pieds).
- Délais : `crossed` après `confirm_frames` images coupées d'affilée (2,
  soit ~22 ms à 90 i/s), `clear` après `release_frames` images entières (5).
- Un passage `crossed` → `blocked` (ou l'inverse) est possible sans repasser
  par `clear`.

### `event service`

```
event service laser=on camera=streaming calibrated=yes watching=yes
```

Envoyé quand l'un de ces champs change (et au nouvel abonné). Le jeu peut
s'en servir pour afficher « détection indisponible » (caméra absente, borne
non calibrée).

## Ce que le jeu doit faire

1. Au démarrage : rien de particulier (l'autoload s'abonne seul) ;
   éventuellement `await ping()` pour vérifier `api=1`.
2. Pendant une partie : `laser(true)`, puis réagir à `line_crossed` (faute
   de pied) ; `is_crossed()` donne l'état courant, par exemple au moment
   d'une touche de fléchette reçue de la cible. `laser(false)` entre les
   parties si la ligne ne doit pas rester allumée.
3. Écran de réglage (installation) : `snapshot()` pour cadrer la caméra,
   `calibrate()` avec personne sur la ligne, afficher la réponse.
4. À l'arrêt : rien. `udg-game.service` peut appeler
   `ExecStopPost=-/usr/bin/udg-throw-line laser off` pour éteindre la ligne
   quand le jeu s'arrête ou plante.

## Outil en ligne de commande

Sur la borne : `udg-throw-line <commande…>` (ou `python3 -m
udg_throw_line.cli` dans le dépôt) envoie une commande et affiche la
réponse ; code retour 0 si `ok`/`pong`, 1 sinon. `udg-throw-line watch`
s'abonne et affiche les événements jusqu'à Ctrl-C.

```bash
udg-throw-line laser on
udg-throw-line calibrate
udg-throw-line watch
```

Sans l'outil : `echo -n "status" | nc -u -w1 127.0.0.1 47810`.
