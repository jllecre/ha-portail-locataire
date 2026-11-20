# Version 0.7.0 - chauffage au sol

## Installation sur HA APPART

1. Sauvegarder la configuration actuelle du portail.
2. Actualiser le magasin d'applications puis mettre � jour le portail existant vers 0.7.0. Le d�p�t et la branche restent identiques.
3. Dans la configuration, activer `floor_heating_enabled: true` puis red�marrer le portail.
4. Conserver les entit�s Z1 � Z4 et laisser `zone5_entity: ""` tant que Z5 n'est pas install�e. Une fois install�e, renseigner son identifiant climate exact.
5. Tester une seule zone avec un code de s�jour valide, en mode propri�taire Arriv�e. Comparer la consigne affich�e dans HA avant/apr�s une petite modification, puis r�tablir la valeur souhait�e.

Le port HTTPS, Cloudflare et les jetons ne changent pas.

## Comportement

- Les temp�ratures actuelles restent visibles toute l'ann�e.
- Les consignes du plancher sont accessibles du 1er octobre au 30 avril inclus, selon Europe/Madrid, uniquement en mode Arriv�e.
- Chaque zone pr�sente la consigne HA, les boutons - et + et un bouton Appliquer.
- Les bornes et le pas proviennent du thermostat HA (pas de repli 0,5 �C si absent). Un thermostat indisponible, arr�t� ou d�pourvu des attributs n�cessaires n'est pas commandable.
- L'�tat MQTT `unknown` n'emp�che pas le r�glage si les attributs du thermostat sont valides. Le probl�me de mode MQTT doit �tre trait� s�par�ment.
- Le serveur contr�le la session, le code CSRF, la saison, le statut de location, l'identifiant de zone et la temp�rature. Une seconde commande sur la m�me zone est limit�e pendant trois secondes.
- Le seul service expos� est `climate.set_temperature`, sur les entit�s configur�es. Aucun changement de mode, marche/arr�t de chaudi�re ou reset de compteur.
- � Demande transmise � signifie que HA a accept� le service. � Consigne confirm�e � indique que HA a ensuite renvoy� la valeur demand�e ; cela ne prouve pas la temp�rature effective du logement.
- Les textes sont disponibles en FR, EN, NL et ES. Les r�glages se r�actualisent toutes les quinze secondes hors saisie en cours.
- L'option est d�sactiv�e par d�faut. En �t�, un message remplace les commandes du plancher ; aucune commande de climatisation n'est encore impl�ment�e.

## �tape climatisation

Pr�voir les m�mes cartes de zones de mai � septembre avec marche/arr�t et consigne, une fois connus les identifiants climate, les modes r�ellement support�s et la correspondance des zones. Si la climatisation est uniquement disponible via une application distincte, pr�voir plut�t un lien vers cette application. Ne pas exposer de commande g�n�rique ou de jeton HA au navigateur.

## Validation de d�veloppement

Tests automatis�s avec client aiohttp local et API HA simul�e : authentification, CSRF, saison et fronti�res horaires Madrid, mode Arriv�e, limites/pas, zone absente, r�vocation de session pendant une lecture, �chec HA, limitation des r�p�titions et traductions. Aucun appareil r�el n'a �t� actionn� pendant le d�veloppement. La validation mat�rielle reste � effectuer sur HA APPART.
