# Détection du franchissement : principe, mise en place, réglages

## 1. Principe

Un module laser « ligne » trace la ligne de lancer au sol. Le faisceau forme
un **plan** qui part du laser et coupe le sol le long de la ligne. Tout objet
qui traverse ce plan (le pied du joueur) intercepte la lumière :

- au sol, la ligne **disparaît** à cet endroit (ombre) ;
- la lumière tombe sur la chaussure, **plus haut** que le sol ; une caméra
  qui ne regarde pas dans le plan du laser la voit **décalée** par rapport à
  la ligne (parallaxe).

La caméra filme la ligne. Le service la découpe en échantillons tous les
`step` pixels et, autour de chacun, lit une courte fenêtre de
2 × `search` + 1 pixels **en travers** de la ligne. Calibrée sans personne,
chaque fenêtre montre un pic lumineux à une position connue (le **décalage
de référence**) avec un **contraste** connu (pic moins le pixel le plus
sombre de la fenêtre : la lumière ambiante s'annule).

Un échantillon est **coupé** si, dans l'image courante :

- son contraste tombe sous `visibility` × référence (ligne masquée), ou
- son pic se déplace de plus de `tolerance` pixels (lumière sur la
  chaussure, encore dans la fenêtre).

Un tronçon d'échantillons coupés d'au moins `min_width` de la ligne est un
**franchissement** (`crossed`). Plus de `max_width` de la ligne coupée d'un
coup : `blocked` (caméra masquée, laser en défaut…). L'état est confirmé sur
quelques images (`confirm_frames`, `release_frames`) pour ignorer un
parasite isolé.

Le service ne lit que les **photosites rouges** de la mosaïque de Bayer
(un par cellule 2 × 2), en brut, sans ISP : le laser rouge y garde tout son
contraste, et la lecture coûte un accès mémoire par pixel. Toutes les
grandeurs en pixels de ce document sont dans ce « plan rouge » (demi
résolution : 320 × 240 pour des images 640 × 480).

Coût mesuré sur le PC de développement (WSL, Python 3.12) : **0,11 ms par
image** pour 130 échantillons. Sur le Cortex-A76 de la ROCK 5C, compter
quelques fois plus (non mesuré) : quelques pour cent d'un cœur à 90 i/s.

## 2. Mise en place physique

- **Parallaxe obligatoire.** La caméra ne doit pas regarder dans le plan du
  laser, sinon la lumière renvoyée par la chaussure retombe exactement sur
  la ligne et le pied devient invisible (un test le documente :
  `test_light_sent_back_in_place_is_invisible`). Idéal : laser au-dessus de
  la ligne (plafond, haut de la borne) qui éclaire vers le bas ; caméra
  décalée sur le côté ou devant, qui voit la ligne en biais.
- Décalage visible sur la chaussure : il doit dépasser `tolerance` (2 px)
  dès quelques centimètres de hauteur. Avec la caméra à 1 m de la ligne et
  640 × 480, un pixel du plan rouge vaut environ 3–5 mm au sol ; une semelle
  de 2 cm de haut décale déjà la lumière de plusieurs pixels si la caméra
  voit le plan sous un angle franc.
- La ligne doit **traverser une bonne partie de l'image** (au moins 15 %,
  idéalement de bord à bord avec une marge) : plus d'échantillons, meilleure
  résolution de position.
- Une ligne **plutôt horizontale** ou **plutôt verticale** dans l'image
  convient (le service essaie les deux) ; l'image peut être retournée
  (`hflip`, `vflip`), seul le sens de `pos` change.
- **Exposition courte** : on veut voir le laser, pas la salle. Un **filtre
  passe-bande** à la longueur d'onde du laser (650 nm typiquement) devant
  l'objectif rendra la détection indifférente à l'éclairage de la salle.
- Sol : un sol mat et clair renvoie mieux le laser qu'un sol brillant
  (reflet spéculaire hors de la caméra) ou noir.

## 3. Réglage pas à pas (sur la borne)

```sh
systemctl status udg-throw-line
udg-throw-line laser on
udg-throw-line snapshot
# depuis le PC : scp root@<borne>:/run/udg-throw-line/snapshot.bmp .
```

1. **Cadrage** : sur la capture, la ligne laser doit être nette, entière,
   loin des bords.
2. **Exposition** : `udg-throw-line camera exposure=300 gain=16`, nouvelle
   capture, recommencer. Viser une ligne bien visible (pas saturée en blanc)
   sur un fond sombre. La capture est étirée jusqu'à × 16 quand l'image est
   sombre : un fond « noir » sur la capture est normal. Reporter les valeurs
   dans `/data/udg-throw-line/udg-throw-line.conf` :

   ```ini
   [camera]
   exposure = 300
   gain = 16
   ```

   puis `systemctl restart udg-throw-line`.
3. **Calibration**, personne sur la ligne : `udg-throw-line calibrate`.
   Réponse attendue : `usable` proche de `samples`, `contrast` au moins
   0,1 (fraction de la pleine échelle). Une nouvelle capture montre les
   points verts de part et d'autre de toute la ligne ; des points magenta
   signalent des parties non surveillées (trop sombres, reflet…).
4. **Essai** : `udg-throw-line watch`, puis franchir la ligne à plusieurs
   endroits, pied posé et pied levé. Chaque passage doit donner
   `state=crossed` puis `state=clear`.
5. Si la recherche automatique échoue (message `no laser line found`), la
   capture montre où est la ligne : l'imposer avec
   `udg-throw-line calibrate line=x0,y0,x1,y1` (coordonnées normalisées,
   0–1, mesurées sur la capture : x = colonne / largeur, y = ligne / hauteur).

À refaire seulement si la caméra, le laser ou le mode caméra (taille,
retournements) changent ; la calibration est liée à ce mode et le service
refuse une calibration faite dans un autre (journal :
`calibration not used (made for …)`).

## 4. Calibration : ce que fait le service

1. Laser éteint : `settle_frames` images ignorées (encore dans la file du
   pilote), puis `frames` images ; laser allumé : de même. La moyenne des
   images allumées moins celle des images éteintes ne garde que le laser :
   un tapis rouge ou une affiche s'annulent. Sans laser commutable
   (`backend = none`), le service travaille sur les seules images allumées,
   en retirant la médiane de chaque colonne.
2. Recherche de la ligne : un pic par colonne (ou par rangée), puis un
   ajustement de droite robuste (droites candidates passant par des paires
   de points, celle qui a le plus de points proches, affinée par moindres
   carrés). Contrôles : assez de points, étendue d'au moins 15 % de
   l'image, au moins la moitié de la longueur visible. Les extrémités sont
   rognées de `margin` échantillons.
3. Référence : chaque échantillon est mesuré sur chaque image (le même code
   que la détection). Il est écarté (« mort ») si son contraste est sous
   `min_contrast`, s'il ne dépasse pas `snr` fois la texture du sol laser
   éteint, si son pic est au bord de la fenêtre ou s'il saute d'une image à
   l'autre. Il faut au moins `min_usable` (60 %) d'échantillons utilisables.

Pendant la surveillance, la référence suit lentement (`adapt`) les
variations douces (laser qui chauffe, lumière qui change, caméra qui bouge
d'un pixel), seulement sur les images où la ligne est entière et seulement
pour les échantillons intacts : un pied immobile n'est jamais appris. Le
contraste de référence reste entre 0,5 × et 2 × la valeur calibrée.

## 5. Paramètres (`[detect]`)

| Clé | Défaut | Effet |
|---|---|---|
| `step` | 2 | pixels entre deux échantillons ; plus petit = plus fin, plus coûteux |
| `search` | 6 | demi-fenêtre en travers ; doit couvrir le tremblement de la ligne mais rester plus court que le décalage sur une chaussure |
| `tolerance` | 2.0 | déplacement de pic encore « intact » |
| `visibility` | 0.5 | contraste minimal relatif ; plus bas = moins sensible aux variations, mais une ombre partielle passe |
| `min_width` | 0.02 | plus petit tronçon compté (2 % de la ligne : ~2 cm sur 1 m) |
| `gap` | 1 | échantillons intacts tolérés dans un tronçon |
| `max_width` | 0.6 | au-delà : `blocked` |
| `confirm_frames` / `release_frames` | 2 / 5 | anti-rebond |
| `settle_frames` | 4 | images ignorées après allumage du laser |
| `adapt` | 0.002 | vitesse de suivi de la référence (0 = figée) |
| `reverse_position` | no | sens de `pos` |

Changer `step` impose de recalibrer ; changer `search` aussi, en pratique.

## 6. Limites connues

- Pas de parallaxe, pas de détection (§ 2).
- Un pied qui passe **au-dessus** du plan sans le couper (saut) n'est pas vu.
- Lumière ambiante très changeante dans le rouge (soleil direct sur la
  ligne) : `adapt` absorbe les variations lentes, pas un flash ; d'où le
  filtre passe-bande recommandé.
- La latence dépend de la cadence : à 90 i/s, `crossed` part environ
  2 images après le contact (~25 ms + latence du pilote), ce qui reste bien
  sous le temps de vol d'une fléchette.
