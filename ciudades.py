"""Lugares que suelen dar nombre a las carpetas de fotos de las giras (España, Portugal y algunos del extranjero).
Se buscan como palabras completas en cada carpeta de la ruta, sin tildes ni mayúsculas. Para añadir más sin tocar
el código: variable FOTOS_CIUDADES_EXTRA (separadas por comas)."""
import os

_ESPANA = """
alava, araba, vitoria, gasteiz, albacete, alicante, alacant, almeria, avila, badajoz, barcelona, bilbao, bizkaia,
vizcaya, burgos, caceres, cadiz, castellon, ciudad real, cordoba, a coruna, la coruna, coruna, cuenca, girona,
gerona, granada, guadalajara, gipuzkoa, guipuzcoa, donostia, san sebastian, huelva, huesca, jaen, leon, lleida,
lerida, logrono, rioja, lugo, madrid, malaga, murcia, navarra, pamplona, iruna, ourense, orense, oviedo, asturias,
gijon, aviles, palencia, palma, mallorca, menorca, ibiza, eivissa, las palmas, gran canaria, pontevedra, vigo,
salamanca, tenerife, santa cruz de tenerife, la gomera, gomera, lanzarote, fuerteventura, la palma, el hierro,
santander, cantabria, segovia, sevilla, soria, tarragona, teruel, toledo, valencia, valladolid, zamora, zaragoza,
ceuta, melilla, merida, cartagena, lorca, elche, alcoy, benidorm, torrevieja, orihuela, gandia, sagunto, torrent,
jerez, algeciras, marbella, estepona, fuengirola, torremolinos, benalmadena, velez malaga, antequera, motril,
linares, ubeda, baeza, andujar, dos hermanas, utrera, ecija, puerto de santa maria, chiclana, san fernando,
sanlucar, la linea, roquetas, el ejido, ponferrada, astorga, aranda de duero, miranda de ebro, talavera,
puertollano, alcazar de san juan, tomelloso, valdepenas, plasencia, don benito, almendralejo, irun, eibar,
barakaldo, getxo, portugalete, durango, basauri, llodio, laudio, tolosa, zarautz, arrasate, mondragon, tudela,
estella, calahorra, jaca, monzon, barbastro, calatayud, ejea, alcaniz, reus, tortosa, sabadell, terrassa,
badalona, hospitalet, mataro, granollers, manresa, figueres, blanes, lloret, sant cugat, castelldefels,
santiago de compostela, santiago, ferrol, alcala de henares, mostoles, fuenlabrada, leganes, getafe, alcorcon,
torrejon, parla, alcobendas, las rozas, pozuelo, majadahonda, boadilla, rivas, coslada, aranjuez, collado villalba,
san sebastian de los reyes, tres cantos, valdemoro, arganda, molina de segura, alcantarilla, yecla, jumilla,
caravaca, aguilas, mazarron, san javier, arona, adeje, la laguna, puerto de la cruz, telde, arrecife,
puerto del rosario, los llanos, benavente, ciudad rodrigo, bejar, medina del campo, la robla, siero, langreo,
mieres, ponteareas, lalin, cieza, totana, vila real, villarreal, onda, benicarlo, vinaros, denia, javea, calpe,
villena, ibi, petrer, novelda, crevillente, santa pola, almansa, hellin, villarrobledo, manzanares, ciudad lineal,
mallorca, pollensa, manacor, inca, mahon, ciutadella, euskadi, pais vasco, galicia, andalucia, extremadura,
castilla, catalunya, cataluna, aragon, canarias, baleares
"""
_EXTRANJERO = """
lisboa, lisbon, porto, oporto, braga, coimbra, aveiro, faro, setubal, evora, leiria, viseu, guimaraes, funchal,
madeira, azores, acores, berlin, paris, londres, london, roma, rome, milan, bruselas, brussels, amsterdam, lyon,
marsella, marseille, burdeos, bordeaux, toulouse, lille, nantes, estrasburgo, strasbourg, munich, hamburgo,
hamburg, frankfurt, colonia, koln, viena, wien, praga, varsovia, budapest, copenhague, kobenhavn, aarhus, odense,
estocolmo, oslo, dublin, ginebra, geneve, zurich, astana, mexico, bogota, lima, buenos aires, ouagadougou, dakar,
abidjan, casablanca, rabat, tanger, marrakech, argel, tunez
"""


def _lista(texto):
    return [x.strip() for x in texto.replace("\n", " ").split(",") if x.strip()]


CIUDADES = sorted(set(_lista(_ESPANA) + _lista(_EXTRANJERO) + _lista(os.getenv("FOTOS_CIUDADES_EXTRA", ""))),
                  key=len, reverse=True)
