# udg-throw-line

Service système qui surveille la **ligne de lancer** de la borne « Ultimate
Darts Game » (Radxa ROCK 5C, RK3588S, image Yocto `udg-yocto`) :

- il pilote la **ligne laser** tracée au sol (une sortie GPIO l'allume et
  l'éteint) ;
- il lit la **caméra CSI** (Raspberry Pi Camera v1.3, OV5647, en Bayer brut
  par le CIF, sans ISP ni rkaiq) qui filme cette ligne ;
- il **signale au jeu** chaque franchissement : un pied qui traverse le plan
  du laser masque ou décale une partie de la ligne.

Le jeu (Godot, dépôt `udg-app`) ne touche jamais au matériel : il envoie des
commandes texte sur **UDP `127.0.0.1:47810`** et s'abonne aux événements
(`event line state=crossed pos=0.42 …`), sur le modèle de
`udg-leds-service`.

- Pour les développeurs du jeu : [`doc/PROTOCOLE.md`](doc/PROTOCOLE.md) et
  l'autoload [`godot/udg_throw_line.gd`](godot/udg_throw_line.gd).
- Principe, installation de la caméra et du laser, réglages :
  [`doc/DETECTION.md`](doc/DETECTION.md).

Choix techniques :

- Python **3.12, bibliothèque standard uniquement**, un seul processus, une
  boucle `selectors` (socket UDP + descripteur de la caméra + minuteries),
  pas de threads.
- Capture **V4L2 multi-plans en mmap écrite en `struct` + `ioctl`** (pas de
  bibliothèque, pas de `ctypes`) ; structures et numéros d'ioctl vérifiés
  contre les en-têtes Linux.
- Laser par une entrée **`gpio-leds`** du device tree (production) ou par
  une ligne du **GPIO character device** (mise au point, n'importe quelle
  broche sans reflasher).
- Tourne sur PC (Linux/WSL, Windows) en **`--simulate`** : caméra
  synthétique avec ligne laser et pied virtuel (`sim foot`), pour développer
  le jeu avec de vrais événements.

## Arborescence

```
udg_throw_line/
  __init__.py      version, version du protocole, port par défaut
  __main__.py      python3 -m udg_throw_line  -> le service
  service.py       boucle selectors, commandes, abonnements, calibration, main()
  cli.py           udg-throw-line : envoie une commande, affiche la réponse ; watch
  protocol.py      analyse des datagrammes (fonctions pures)
  v4l2.py          capture V4L2 (CIF rkcif + sous-périphérique du capteur)
  frame.py         adressage des pixels Bayer (raw16/raw8/mipi10), rendu BMP
  detection.py     échantillons de la ligne, mesure par image, machine d'état
  calibration.py   recherche de la ligne, référence, persistance
  laser.py         sorties laser : gpio-leds (sysfs), GPIO uAPI v2, aucune
  simulator.py     caméra simulée (--simulate)
  config.py        configuration INI en couches
  log.py           journal minimal sur stderr (préfixes de priorité systemd)
conf/udg-throw-line.conf    configuration commentée (-> /etc/udg-throw-line.conf)
systemd/udg-throw-line.service
godot/udg_throw_line.gd     autoload Godot 4 d'exemple
doc/PROTOCOLE.md            protocole UDP, pour les développeurs du jeu
doc/DETECTION.md            principe, mise en place physique, réglages
tests/                      tests unittest (bibliothèque standard)
Makefile                    install (DESTDIR/PREFIX), test, simulate
```

## Lancer sur le PC de développement

Aucune dépendance. Depuis la racine du dépôt :

```bash
make simulate
# ou :
python3 -m udg_throw_line --simulate --verbose --config conf/udg-throw-line.conf \
    --data-dir _install/data --run-dir _install/run
```

Puis, dans un autre terminal (ou depuis le jeu lancé sur le PC) :

```bash
python3 -m udg_throw_line.cli laser on
python3 -m udg_throw_line.cli calibrate
python3 -m udg_throw_line.cli watch          # événements, Ctrl-C pour quitter
python3 -m udg_throw_line.cli sim foot pos=0.4 width=0.1 duration=0.8
python3 -m udg_throw_line.cli snapshot       # -> _install/run/snapshot.bmp
```

`watch` affiche alors `event line state=crossed seq=2 pos=0.397 from=0.347
to=0.447 runs=1`, puis `state=clear … duration=0.90`. La scène simulée
(sol texturé, ligne laser dessinée seulement quand le laser est allumé,
lumière décalée sur la chaussure) fait tourner la vraie calibration et la
vraie détection. Sous Windows, remplacer `python3` par `py -3`.
`--data-dir` / `--run-dir` évitent d'écrire dans `/data` et `/run`
(`_install/` est ignoré par git).

## Tests

```bash
python3 -m unittest            # ou : make test
```

Dans WSL (même version de Python que la cible) :

```bash
wsl -d Ubuntu-yocto -- bash -c "cd /mnt/d/github/udg-godot/udg-throw-line && python3 -m unittest"
```

114 tests, une dizaine de secondes, sous WSL (Python 3.12) comme sous
Windows (Python 3.14 ; l'arrêt propre sur SIGTERM n'y est pas vérifié). Couverture : configuration (couches,
valeurs invalides, fichier livré = valeurs par défaut), protocole (chaque
commande, valide et invalide), adressage des pixels (trois dispositions
mémoire × quatre ordres de Bayer, pixel par pixel), en-tête BMP, capture
V4L2 contre un **faux rkcif** (négociation 640 × 480, ordre de Bayer qui
suit les retournements, mode mémoire 16 bits entre deux `S_FMT`, repli
« compact », file de tampons, image en erreur, périphérique perdu), tailles
de structures et numéros d'ioctl (V4L2 et GPIO), requête GPIO octet par
octet, sortie `gpio-leds`, calibration sur images simulées (ligne
horizontale et verticale à moins d'un pixel, objet rouge ignoré, sans images
laser éteint, ligne imposée, ligne absente ou trop faible, persistance),
détection (pied, pied étroit ignoré, deux pieds, `gap`, `blocked`,
échantillons morts, suivi borné de la référence), machine d'état, service
complet en horloge simulée (calibration, abonnements, événements,
expiration, redémarrage d'une caméra figée, caméra absente réessayée,
capture), et un test d'intégration qui lance le vrai service en
`--simulate` et le pilote par l'outil `udg-throw-line`.

## Configuration

Fichier INI commenté : [`conf/udg-throw-line.conf`](conf/udg-throw-line.conf),
installé en `/etc/udg-throw-line.conf`. Lu en couches, chacune ne
surchargeant que les clés qu'elle contient : `/etc/udg-throw-line.conf`,
puis `/data/udg-throw-line/udg-throw-line.conf` (réglages propres à une
borne : GPIO du laser, exposition…), puis la ligne de commande (`--port`,
`--set section.clé=valeur`). Une valeur invalide est journalisée et ignorée.

| Section | Clés |
|---|---|
| `[camera]` | `video`, `subdev` (`auto`, chemin, `none`), `width`, `height`, `bayer`, `memory_mode` (`word_low`, `word_high`, `compact`, `keep`), `buffers`, `hflip`, `vflip`, `exposure`, `gain`, `retry`, `frame_timeout` |
| `[laser]` | `backend` (`led`, `gpio`, `none`), `led`, `chip`, `line`, `active_low`, `startup` |
| `[detect]` | `step`, `search`, `tolerance`, `visibility`, `min_width`, `gap`, `max_width`, `confirm_frames`, `release_frames`, `settle_frames`, `adapt`, `reverse_position` |
| `[calibration]` | `frames`, `settle_frames`, `min_contrast`, `snr`, `min_usable`, `margin` |
| `[network]` | `address` (bouclage uniquement), `port`, `subscription_ttl`, `max_subscribers` |
| `[service]` | `data_dir`, `run_dir` |
| `[simulate]` | `fps`, `line`, `intensity`, `noise` |

## Outil `udg-throw-line`

```
udg-throw-line [--host H] [--port P] [--timeout S] <commande…>
udg-throw-line watch
```

Envoie un datagramme et affiche la réponse (attente 1 s, 15 s pour
`calibrate`, 5 s pour `snapshot`) ; code retour **0** si la réponse commence
par `ok` ou `pong`, **1** sinon. `watch` s'abonne, affiche les événements
horodatés et renouvelle l'abonnement jusqu'à Ctrl-C. L'adresse et le port
sont lus dans la configuration.

## Intégration dans l'image

### Installation

```bash
make install DESTDIR=/chemin/racine PREFIX=/usr SYSTEMD_UNITDIR=/lib/systemd/system
```

| Installé | Contenu |
|---|---|
| `/usr/lib/udg-throw-line/udg_throw_line/` | le paquet Python |
| `/usr/lib/udg-throw-line/README.md` | ce fichier (référencé par `Documentation=`) |
| `/usr/bin/udg-throw-line-service` | lanceur du service (`--laser-off` : éteint le laser et sort) |
| `/usr/bin/udg-throw-line` | lanceur de l'outil |
| `${SYSTEMD_UNITDIR}/udg-throw-line.service` | unité systemd |
| `/etc/udg-throw-line.conf` | configuration |

Les lanceurs sont de petits scripts Python qui ajoutent
`/usr/lib/udg-throw-line` à `sys.path`. Rien n'est compilé ; l'unité met
`PYTHONDONTWRITEBYTECODE=1` (rootfs en lecture seule).

### Recette Yocto (à créer dans `udg-yocto`)

Même forme que `udg-leds-service_git.bb` :

```bitbake
SRC_URI = "git://github.com/christopheseyler/udg-throw-line.git;protocol=https;branch=main"
SRCREV = "<commit>"
PV = "0.1.0+git"
S = "${WORKDIR}/git"
inherit systemd
do_compile[noexec] = "1"
do_install() {
    oe_runmake install DESTDIR=${D} PREFIX=${prefix} BINDIR=${bindir} \
        LIBDIR=${libdir}/udg-throw-line SYSCONFDIR=${sysconfdir} \
        SYSTEMD_UNITDIR=${systemd_system_unitdir} PYTHON=${bindir}/python3
}
SYSTEMD_SERVICE:${PN} = "udg-throw-line.service"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"
FILES:${PN} += "${libdir}/udg-throw-line"
RDEPENDS:${PN} = "python3-core python3-io python3-fcntl python3-mmap"
```

### `RDEPENDS` Yocto exacts

```
python3-core python3-io python3-fcntl python3-mmap
```

Relevé, pas déduit : un script a exécuté les chemins de code de la cible
(configuration, service complet en caméra simulée avec calibration,
détection et capture BMP, socket UDP, `selectors`, `argparse` du service et
de l'outil, sortie `gpio-leds`) avec le Python 3.12 de WSL, importé `fcntl`
et `mmap` (chargés seulement sur le chemin matériel), relevé `sys.modules`,
puis associé chaque module à son paquet dans
`~/rock5c-yocto/poky/meta/recipes-devtools/python/python3/python3-manifest.json`
(scarthgap) :

| Paquet | Modules réellement chargés |
|---|---|
| `python3-core` | `argparse`, `configparser`, `dataclasses`, `selectors`, `select`, `signal`, `struct`, `array`, `math`, `operator`, `ipaddress`, `re`, `os`, `errno`, `shutil` (par `argparse`), `inspect`/`ast`/`dis` (par `dataclasses`), `zlib`… |
| `python3-io` | `socket` (`_socket`) |
| `python3-fcntl` | `fcntl` (ioctl V4L2 et GPIO) |
| `python3-mmap` | `mmap` (tampons de capture) |

Absents à dessein : `python3-math` (`random` n'est pas utilisé : le
simulateur a son propre générateur xorshift), `python3-logging`,
`python3-json`, `python3-asyncio`, `python3-threading`, `python3-ctypes`
(l'adresse du tableau `v4l2_plane` vient de `array.buffer_info()`). Les tests
(`unittest`, `subprocess`, `ctypes` pour le faux pilote) ne vont pas sur la
cible.

### Unité systemd

[`systemd/udg-throw-line.service`](systemd/udg-throw-line.service), chaque
option commentée : `Type=simple`, `Restart=always`,
`Before=udg-game.service`, `ExecStopPost=… --laser-off` (laser éteint même
après un plantage), `RuntimeDirectory=udg-throw-line` (captures),
`Nice=-5`, durcissement `ProtectSystem=strict`,
`ReadWritePaths=-/data/udg-throw-line`, `DevicePolicy=closed` +
`DeviceAllow=char-video4linux rw` + `DeviceAllow=char-gpiochip rw`,
`RestrictAddressFamilies=AF_INET`. **Pas** de `ProtectKernelTunables=` :
il rend tout `/sys` en lecture seule, or la sortie `gpio-leds` s'écrit dans
`/sys/class/leds/udg-laser/brightness`.

### Device tree : sortie du laser (dans `udg-yocto`)

Le service attend par défaut une LED `udg-laser` (`[laser] backend = led`).
Une fois la broche choisie, l'ajouter au nœud `udg-lights` du patch
`0001-…-SPI4-spidev-for-the-LED-strip.patch` (ou dans un nœud
`gpio-leds` à part) :

```dts
laser {
	label = "udg-laser";
	gpios = <&gpio1 RK_PA4 GPIO_ACTIVE_HIGH>;	/* exemple : broche 26 */
	default-state = "off";
};
```

Mêmes règles que pour les éclairages (`doc/LIGHTING-INTERFACE.md` de
`udg-yocto`) : ni la broche 7 (recovery) ni 19/21/23 (ruban LED), une broche
dont l'état au reset correspond à « éteint », un MOSFET entre la broche et
le module laser, `GPIO_ACTIVE_LOW` pour un étage inverseur. En attendant,
`[laser] backend = gpio` + `chip` + `line` dans
`/data/udg-throw-line/udg-throw-line.conf` permet d'essayer n'importe quelle
broche sans reflasher.

## Procédure de validation sur la borne (à faire)

Rien n'a encore tourné sur le matériel. Dans l'ordre :

1. **Caméra** (voir aussi `doc/CAMERA-INTERFACE.md` § 5 de `udg-yocto`) :
   `dmesg | grep -i ov5647`, `journalctl -u udg-throw-line -b` doit montrer
   `camera /dev/videoN: 640x480 GB10, bytesperline 1280, raw16, Bayer GBRG,
   CIF memory mode word_low`. Si la ligne parle de `compact`, l'ioctl privé
   du rkcif a échoué : le dire dans le journal et le vérifier.
2. **Service** : `udg-throw-line ping` → `pong 0.1.0 api=1 … camera=streaming`,
   `udg-throw-line status` → `fps` proche de 90.
3. **Image** : `udg-throw-line snapshot`, copier
   `/run/udg-throw-line/snapshot.bmp` sur le PC : couleurs plausibles (le
   rouge du laser **rouge** : sinon l'ordre de Bayer est faux, le forcer avec
   `[camera] bayer`), image dans le bon sens (`hflip`/`vflip`).
4. **Laser** : `udg-throw-line laser on` / `off`, à l'œil ; avec
   `backend = gpio`, `gpioinfo` montre la ligne tenue par `udg-throw-line`.
   `systemctl stop udg-throw-line` puis `kill -9` du service : le laser doit
   s'éteindre (`ExecStopPost=`).
5. **Réglage et calibration** : `doc/DETECTION.md` § 3.
6. **Détection** : `udg-throw-line watch` et franchir la ligne ; mesurer la
   charge (`top`) et vérifier qu'aucune image n'est perdue (`frames`
   augmente de ~90 par seconde).
7. **Durcissement** : `systemd-analyze verify udg-throw-line.service`,
   `systemd-analyze security udg-throw-line`.

## Décisions prises

1. **Transport** : UDP local comme `udg-leds-service` (Godot a
   `PacketPeerUDP`, pas de socket Unix). Port 47810.
2. **Événements par abonnement** renouvelé (`subscribe`, TTL 10 s, l'autoload
   renouvelle toutes les 3 s) : pas d'état à nettoyer si le jeu plante, et
   réabonnement automatique si le service redémarre. Un nouvel abonné reçoit
   l'état courant.
3. **Réponses avec le mot-clé** (`ok calibrate …`) : `calibrate` et
   `snapshot` répondent en différé, le client doit pouvoir les reconnaître.
4. **Capture en Bayer brut, photosites rouges seuls** : pas de dématriçage,
   pas d'ISP ; la mesure par image tient en un `operator.itemgetter` et trois
   appels C par échantillon.
5. **Mode mémoire 16 bits du rkcif** (`word_low`) plutôt que le RAW10
   « compact » par défaut, dont l'ordre des bits n'est pas documenté ; repli
   sur une lecture « MIPI » (octets de poids fort) si l'ioctl échoue.
6. **Ordre de Bayer lu sur le capteur** après les retournements (l'OV5647
   annonce SGBRG10 avec les retournements par défaut, pas le SBGGR10 de sa
   table de modes).
7. **640 × 480 à 90 i/s** par défaut : champ complet (binning), latence
   faible, toutes les images analysées.
8. **Calibration laser éteint / allumé** par le service lui-même : la
   différence ne garde que le laser.
9. **Détection = ligne masquée ou décalée** sur un tronçon d'au moins
   `min_width`, anti-rebond sur quelques images ; `blocked` au-delà de
   `max_width` pour ne pas compter une caméra masquée comme une faute.
10. **Suivi lent de la référence** seulement quand la ligne est entière, borné
    à 0,5–2 × la calibration.
11. **Calibration liée au mode caméra** (taille, format, retournements) et à
    `step` ; refusée sinon, avec un message dans le journal.
12. **Laser** : `gpio-leds` en production (le noyau tient la ligne éteinte dès
    le démarrage, `ExecStopPost=` peut l'éteindre), GPIO uAPI v2 pour la mise
    au point ; état demandé retenu et réappliqué quand la sortie revient.
13. **Simulation complète** (`--simulate`) plutôt qu'un simple mode « sans
    caméra » : le jeu reçoit de vrais événements et les tests couvrent toute
    la chaîne.

## Non vérifié / limites connues

- **Rien n'a tourné sur la carte** : ni la capture réelle (les ioctl sont
  testés contre un faux pilote écrit d'après les sources du noyau vendor),
  ni l'ioctl `RKCIF_CMD_SET_CSI_MEMORY_MODE`, ni la sortie laser, ni l'unité
  systemd (`DeviceAllow=char-gpiochip` et `char-video4linux` supposent ces
  noms dans `/proc/devices`).
- La lecture du RAW10 « compact » suppose l'empaquetage MIPI CSI-2
  (non vérifié sur le rkcif) : le mode `word_low` l'évite.
- Le coût CPU sur la carte n'est pas mesuré (0,11 ms par image sur le PC).
- `godot/udg_throw_line.gd` n'a pas été exécuté dans Godot.
- Limites de la méthode (parallaxe nécessaire, saut au-dessus du plan,
  soleil direct) : `doc/DETECTION.md` § 6.
