# Version 0.7.0 — chauffage au sol

## Installation sur HA APPART

1. Sauvegarder la configuration actuelle du portail.
2. Actualiser le magasin d'applications puis mettre à jour le portail existant vers 0.7.0. Le dépôt et la branche restent identiques.
3. Dans la configuration, activer `floor_heating_enabled: true` puis redémarrer le portail.
4. Conserver les entités Z1 à Z4 et laisser `zone5_entity: ""` tant que Z5 n'est pas installée. Une fois installée, renseigner son identifiant climate exact.
5. Tester une seule zone avec un code de séjour valide, en mode propriétaire Arrivée. Comparer la consigne affichée dans HA avant/après une petite modification, puis rétablir la valeur souhaitée.

Le port HTTPS, Cloudflare et les jetons ne changent pas.

## Comportement

- Les températures actuelles restent visibles toute l'année.
- Les consignes du plancher sont accessibles du 1er octobre au 30 avril inclus, selon Europe/Madrid, uniquement en mode Arrivée.
- Chaque zone présente la consigne HA, les boutons − et + et un bouton Appliquer.
- Les bornes et le pas proviennent du thermostat HA (pas de repli 0,5 °C si absent). Un thermostat indisponible, arrêté ou dépourvu des attributs nécessaires n'est pas commandable.
- L'état MQTT `unknown` n'empêche pas le réglage si les attributs du thermostat sont valides. Le problème de mode MQTT doit être traité séparément.
- Le serveur contrôle la session, le code CSRF, la saison, le statut de location, l'identifiant de zone et la température. Une seconde commande sur la même zone est limitée pendant trois secondes.
- Le seul service exposé est `climate.set_temperature`, sur les entités configurées. Aucun changement de mode, marche/arrêt de chaudière ou reset de compteur.
- « Demande transmise » signifie que HA a accepté le service. « Consigne confirmée » indique que HA a ensuite renvoyé la valeur demandée ; cela ne prouve pas la température effective du logement.
- Les textes sont disponibles en FR, EN, NL et ES. Les réglages se réactualisent toutes les quinze secondes hors saisie en cours.
- L'option est désactivée par défaut. En été, un message remplace les commandes du plancher ; aucune commande de climatisation n'est encore implémentée.

## Étape climatisation

Prévoir les mêmes cartes de zones de mai à septembre avec marche/arrêt et consigne, une fois connus les identifiants climate, les modes réellement supportés et la correspondance des zones. Si la climatisation est uniquement disponible via une application distincte, prévoir plutôt un lien vers cette application. Ne pas exposer de commande générique ou de jeton HA au navigateur.

## Validation de développement

Tests automatisés avec client aiohttp local et API HA simulée : authentification, CSRF, saison et frontières horaires Madrid, mode Arrivée, limites/pas, zone absente, révocation de session pendant une lecture, échec HA, limitation des répétitions et traductions. Aucun appareil réel n'a été actionné pendant le développement. La validation matérielle reste à effectuer sur HA APPART.
