#!/usr/bin/env python3
"""
merge_epg.py
Legge tutti i file .gz EPG dalla cartella epg/, normalizza i channel id
tramite la mappa degli alias, e produce epg/merged_epg.xml.gz scegliendo
per ogni canale la fonte con più programmi nella giornata corrente.
"""

import os
import gzip
import glob
import sys
from collections import defaultdict
from lxml import etree

# ---------------------------------------------------------------------------
# MAPPA ALIAS  →  channel_id canonico (parte prima dei ":" in riferimento.txt)
# Ogni alias (in qualsiasi variante di scrittura trovata nelle sorgenti EPG)
# viene normalizzato a lower-strip per il matching, ma l'id canonico viene
# tenuto esattamente come indicato sotto.
# ---------------------------------------------------------------------------
CHANNEL_ALIASES: dict[str, str] = {
    # Rai 1
    "rai 1": "Rai 1", "rai1": "Rai 1", "rai 1 hd": "Rai 1", "rai 1 fhd": "Rai 1",
    "rai 1 full hd": "Rai 1", "rai 1 sd": "Rai 1", "rai.1.hd..101.it": "Rai 1",
    "rai1 hd": "Rai 1", "rai1.it": "Rai 1", "rai 4k.it": "Rai 1", "rai 1 hd.it": "Rai 1",
    "rai 1.it": "Rai 1", "rai1.it": "Rai 1", "rai.1.it": "Rai 1",
    # Rai 2
    "rai 2": "Rai 2", "rai2": "Rai 2", "rai 2 hd": "Rai 2", "rai 2 fhd": "Rai 2",
    "rai 2 full hd": "Rai 2", "rai 2 sd": "Rai 2", "rai.2.hd..102.it": "Rai 2",
    "rai2 hd": "Rai 2", "rai2.it": "Rai 2", "rai 2 hd.it": "Rai 2",
    # Rai 3
    "rai 3": "Rai 3", "rai3": "Rai 3", "rai 3 hd": "Rai 3", "rai 3 fhd": "Rai 3",
    "rai 3 full hd": "Rai 3", "rai 3 sd": "Rai 3", "rai.3.hd..103.it": "Rai 3",
    "rai3 hd": "Rai 3", "rai3.it": "Rai 3", "rai 3 hd.it": "Rai 3",
    # Rete 4
    "rete 4": "Rete 4", "rete4": "Rete 4", "rete 4 hd": "Rete 4", "rete 4 fhd": "Rete 4",
    "rete 4 full hd": "Rete 4", "rete 4 sd": "Rete 4", "rete.4.it": "Rete 4",
    "rete4.it": "Rete 4", "rete 4 hd.it": "Rete 4",
    # Canale 5
    "canale 5": "Canale 5", "canale5": "Canale 5", "canale 5 hd": "Canale 5",
    "canale 5 fhd": "Canale 5", "canale 5 full hd": "Canale 5", "canale 5 sd": "Canale 5",
    "canale.5.it": "Canale 5", "canale5.it": "Canale 5", "canale 5.it": "Canale 5",
    "canale 5 hd.it": "Canale 5",
    # Italia 1
    "italia 1": "Italia 1", "italia1": "Italia 1", "italia 1 hd": "Italia 1",
    "italia 1 fhd": "Italia 1", "italia 1 full hd": "Italia 1", "italia 1 sd": "Italia 1",
    "italia uno.it": "Italia 1", "italia.1.it": "Italia 1", "italia1.it": "Italia 1",
    "italia 1 hd.it": "Italia 1",
    # LA7
    "la7": "LA7", "la 7": "LA7", "la7 hd": "LA7", "la 7 hd": "LA7",
    "la 7 fhd": "LA7", "la 7 full hd": "LA7", "la 7 sd": "LA7",
    "la7.it": "LA7", "la 7.it": "LA7", "la 7.it": "LA7", "la 7d.it": "LA7",
    "la7.hd.it": "LA7", "la7 hd.it": "LA7", "la 7.it": "LA7",
    # TV8
    "tv8": "TV8", "tv 8": "TV8", "tv8 hd": "TV8", "tv 8 hd": "TV8",
    "tv 8 fhd": "TV8", "tv 8 full hd": "TV8", "tv 8 sd": "TV8",
    "tv8.it": "TV8", "tv8 hd.it": "TV8", "tv 8.it": "TV8", "tv8.hd.it": "TV8",
    # Nove
    "nove": "Nove", "nove hd": "Nove", "nove fhd": "Nove", "nove full hd": "Nove",
    "nove sd": "Nove", "nove.it": "Nove", "nove hd.it": "Nove",
    "nove.hd..149.it": "Nove",
    # Rai 4
    "rai 4": "Rai 4", "rai4": "Rai 4", "rai 4 hd": "Rai 4", "rai 4 fhd": "Rai 4",
    "rai 4 full hd": "Rai 4", "rai 4 sd": "Rai 4", "rai.4..5021.it": "Rai 4",
    "rai4.it": "Rai 4", "rai 4.it": "Rai 4",
    # Iris
    "iris": "Iris", "iris hd": "Iris", "iris hd.it": "Iris", "iris.it": "Iris",
    # La 5
    "la 5": "La 5", "la5": "La 5", "la 5 hd": "La 5", "la 5 fhd": "La 5",
    "la 5 full hd": "La 5", "la 5 sd": "La 5", "la5.it": "La 5",
    "la5 hd.it": "La 5", "la.5.it": "La 5", "la 5.it": "La 5", "la 5 hd.it": "La 5",
    # Rai 5
    "rai 5": "Rai 5", "rai5": "Rai 5", "rai 5 hd": "Rai 5", "rai 5 fhd": "Rai 5",
    "rai 5 full hd": "Rai 5", "rai 5 sd": "Rai 5", "rai.5..5023.it": "Rai 5",
    "rai5.it": "Rai 5", "rai 5.it": "Rai 5",
    # Rai Movie
    "rai movie": "Rai Movie", "raimovie": "Rai Movie", "rai movie hd": "Rai Movie",
    "rai movie fhd": "Rai Movie", "rai movie full hd": "Rai Movie", "rai movie sd": "Rai Movie",
    "rai.movie..5024.it": "Rai Movie", "raimovie.it": "Rai Movie",
    # Rai Premium
    "rai premium": "Rai Premium", "raipremium": "Rai Premium", "rai premium hd": "Rai Premium",
    "rai premium fhd": "Rai Premium", "rai premium full hd": "Rai Premium",
    "rai premium sd": "Rai Premium", "rai.premium..5025.it": "Rai Premium",
    "raipremium.it": "Rai Premium", "rai premium.it": "Rai Premium",
    # Italia 2
    "italia 2": "Italia 2", "italia2": "Italia 2", "italia 2 hd": "Italia 2",
    "italia 2 fhd": "Italia 2", "italia 2 full hd": "Italia 2", "italia 2 sd": "Italia 2",
    "italia.2.it": "Italia 2", "italia2.it": "Italia 2", "mediaset italia due.it": "Italia 2",
    # Mediaset Extra
    "mediaset extra": "Mediaset Extra", "mediaset extra hd": "Mediaset Extra",
    "mediaset extra fhd": "Mediaset Extra", "mediaset extra full hd": "Mediaset Extra",
    "mediaset extra sd": "Mediaset Extra", "mediaset extra.it": "Mediaset Extra",
    "mediasetextra.it": "Mediaset Extra", "mediaset.extra.it": "Mediaset Extra",
    "mediaset extra hd.it": "Mediaset Extra",
    # TV2000
    "tv2000": "TV2000", "tv 2000": "TV2000", "tv2000 hd": "TV2000",
    "tv2000 fhd": "TV2000", "tv2000.it": "TV2000", "tv 2000.it": "TV2000",
    "tv2000 hd.it": "TV2000",
    # Cielo
    "cielo": "Cielo", "cielo hd": "Cielo", "cielo fhd": "Cielo",
    "cielo full hd": "Cielo", "cielo sd": "Cielo", "cielo.it": "Cielo",
    # 20
    "20": "20", "20 mediaset": "20", "canale 20": "20", "20.it": "20",
    "20 mediaset hd": "20", "20 mediaset fhd": "20", "20 mediaset full hd": "20",
    "20 mediaset sd": "20", "20mediaset hd.it": "20", "mediaset 20.it": "20",
    "canale 20.it": "20", "mediaset20.it": "20",
    # Rai Sport
    "rai sport": "Rai Sport", "raisport": "Rai Sport", "rai sport 1": "Rai Sport",
    "rai sport hd": "Rai Sport", "rai sport fhd": "Rai Sport",
    "rai sport full hd": "Rai Sport", "rai sport sd": "Rai Sport",
    "rai sport + hd": "Rai Sport", "raisport1": "Rai Sport",
    "raisport1.it": "Rai Sport", "rai sport1.it": "Rai Sport",
    "rai sport hd.it": "Rai Sport", "rai sport + hd.it": "Rai Sport",
    "raisport.it": "Rai Sport", "rai.sport...227.it": "Rai Sport",
    # Focus
    "focus": "Focus", "focus hd.it": "Focus", "focus.it": "Focus",
    # Rai Storia
    "rai storia": "Rai Storia", "raistoria": "Rai Storia", "rai storia hd": "Rai Storia",
    "rai storia fhd": "Rai Storia", "rai storia full hd": "Rai Storia",
    "rai storia sd": "Rai Storia", "rai.storia..5054.it": "Rai Storia",
    "raistoria.it": "Rai Storia",
    # Rai News 24
    "rai news 24": "Rai News 24", "rainews24": "Rai News 24", "rai news 24 hd": "Rai News 24",
    "rai news.it": "Rai News 24", "rai.news.24..508.it": "Rai News 24",
    "rainews24.it": "Rai News 24", "rai news 24 hd.it": "Rai News 24",
    "rainews.it": "Rai News 24", "rai news 24.it": "Rai News 24",
    "rai 24 news.it": "Rai News 24",
    # TGcom24
    "tgcom24": "TGcom24", "tg com 24": "TGcom24", "tgcom24 hd": "TGcom24",
    "tgcom24.it": "TGcom24", "tgcom24 hd.it": "TGcom24",
    "mediaset tgcom24.it": "TGcom24", "tgcom.it": "TGcom24",
    # Rai Scuola
    "rai scuola": "Rai Scuola", "raiscuola": "Rai Scuola", "rai scuola hd": "Rai Scuola",
    "rai scuola fhd": "Rai Scuola", "rai scuola full hd": "Rai Scuola",
    "rai scuola sd": "Rai Scuola", "raiscuola.it": "Rai Scuola", "rai scuola.it": "Rai Scuola",
    # TwentySeven
    "twentyseven": "TwentySeven", "twenty seven": "TwentySeven",
    "27twentyseven": "TwentySeven", "27twentyseven hd.it": "TwentySeven",
    "mediaset 27.it": "TwentySeven", "twentyseven.it": "TwentySeven",
    "twenty seven.it": "TwentySeven", "27.twentyseven.it": "TwentySeven",
    "mediaset27twentyseven.it": "TwentySeven", "mediaset italia2 hd.it": "TwentySeven",
    # DMAX
    "dmax": "DMAX", "dmax hd": "DMAX", "dmax italia.it": "DMAX",
    "dmax.hd..170.it": "DMAX", "dmax.it": "DMAX", "d max.it": "DMAX",
    "dmax hd.it": "DMAX", "dmax fhd": "DMAX", "dmax full hd": "DMAX", "dmax sd": "DMAX",
    # La7 Cinema
    "la7 cinema": "La7 Cinema", "la7d": "La7 Cinema", "la 7d": "La7 Cinema",
    "la 7d hd": "La7 Cinema", "la 7d fhd": "La7 Cinema", "la 7d full hd": "La7 Cinema",
    "la 7d sd": "La7 Cinema", "la7 cinema.it": "La7 Cinema",
    "la7.cinema.it": "La7 Cinema", "la7d.it": "La7 Cinema",
    # wedotv Movies
    "wedotv movies": "wedotv Movies", "wedo tv": "wedotv Movies", "wedo.it": "wedotv Movies",
    # Real Time
    "real time": "Real Time", "realtime": "Real Time", "real time hd": "Real Time",
    "real time fhd": "Real Time", "real time full hd": "Real Time", "real time sd": "Real Time",
    "real.time.hd..160.it": "Real Time", "realtime.it": "Real Time",
    "real time.it": "Real Time", "real time hd.it": "Real Time",
    # QVC
    "qvc": "QVC", "qvc.it": "QVC", "qvc.it": "QVC",
    # Food Network
    "food network": "Food Network", "food network hd": "Food Network",
    "food network fhd": "Food Network", "food network full hd": "Food Network",
    "food network sd": "Food Network", "food network.it": "Food Network",
    "foodnetwork.it": "Food Network", "food.network.hd..417.it": "Food Network",
    "food network hd.it": "Food Network",
    # Cine34
    "cine34": "Cine34", "cine34 hd": "Cine34", "cine34 hd.it": "Cine34",
    "cine34.it": "Cine34", "cine 34.it": "Cine34",
    # Radio Italia TV
    "radio italia tv": "Radio Italia TV", "radioitaliatv": "Radio Italia TV",
    "radioitaliatv.it": "Radio Italia TV", "radio italia tv hd.it": "Radio Italia TV",
    # RTL 102.5 TV
    "rtl 102.5 tv": "RTL 102.5 TV", "rtl 102.5 hd": "RTL 102.5 TV",
    "rtl 102.5.it": "RTL 102.5 TV", "rtl1025.it": "RTL 102.5 TV",
    "rtl.102.5.hd.it": "RTL 102.5 TV", "rtl 102.5 tv.it": "RTL 102.5 TV",
    "rtl 102.5 hd.it": "RTL 102.5 TV",
    # Discovery
    "discovery": "Discovery", "discovery channel": "Discovery",
    "discovery channel hd": "Discovery", "discovery hd": "Discovery",
    "discovery fhd": "Discovery", "discovery full hd": "Discovery",
    "discovery sd": "Discovery", "discovery.it": "Discovery",
    "discovery channel hd.it": "Discovery", "discovery channel.it": "Discovery",
    "warner tv.it": "Discovery", "warner tv.it": "Discovery",
    "warnertv.it": "Discovery", "discovery.channel.it": "Discovery",
    # Giallo
    "giallo": "Giallo", "giallo hd": "Giallo", "giallo fhd": "Giallo",
    "giallo full hd": "Giallo", "giallo sd": "Giallo", "giallo.it": "Giallo",
    "giallo tv.it": "Giallo", "giallo.tv.it": "Giallo",
    "giallo hd.it": "Giallo", "giallo.hd..167.it": "Giallo",
    # Top Crime
    "top crime": "Top Crime", "topcrime": "Top Crime", "top crime hd": "Top Crime",
    "topcrime hd": "Top Crime", "top crime fhd": "Top Crime",
    "top crime full hd": "Top Crime", "top crime sd": "Top Crime",
    "top crime.it": "Top Crime", "topcrime.it": "Top Crime",
    "topcrime hd.it": "Top Crime", "topcrime.hd..168.it": "Top Crime",
    # Boing
    "boing": "Boing", "boing hd": "Boing", "boing fhd": "Boing",
    "boing full hd": "Boing", "boing sd": "Boing", "boing plus": "Boing",
    "boing.it": "Boing", "boing plus.it": "Boing",
    # Cartoonito
    "cartoonito": "Cartoonito", "cartoonito hd": "Cartoonito",
    "cartoonito.it": "Cartoonito", "cartoonito dtt.it": "Cartoonito",
    # Rai Gulp
    "rai gulp": "Rai Gulp", "raigulp": "Rai Gulp", "rai gulp hd": "Rai Gulp",
    "rai gulp fhd": "Rai Gulp", "rai gulp full hd": "Rai Gulp", "rai gulp sd": "Rai Gulp",
    "rai.gulp..5042.it": "Rai Gulp", "raigulp.it": "Rai Gulp", "rai gulp.it": "Rai Gulp",
    # Rai Yoyo
    "rai yoyo": "Rai Yoyo", "raiyoyo": "Rai Yoyo", "rai yoyo hd": "Rai Yoyo",
    "rai yoyo fhd": "Rai Yoyo", "rai yoyo full hd": "Rai Yoyo", "rai yoyo sd": "Rai Yoyo",
    "rai.yoyo..5043.it": "Rai Yoyo", "raiyoyo.it": "Rai Yoyo", "rai yoyo.it": "Rai Yoyo",
    # Frisbee
    "frisbee": "Frisbee", "frisbee.it": "Frisbee", "-frisbee-.it": "Frisbee",
    # K2
    "k2": "K2", "k2.it": "K2",
    # Super!
    "super!": "Super!", "super!.it": "Super!", "super.it": "Super!",
    # ARTE
    "arte": "ARTE", "arte.it": "ARTE",
    # Mezzo
    "mezzo": "Mezzo", "mezzo.it": "Mezzo",
    # RDS Social TV
    "rds social tv": "RDS Social TV", "rds social tv.it": "RDS Social TV",
    # EQUtv
    "equtv": "EQUtv", "equ tv.it": "EQUtv", "equtv.it": "EQUtv", "equtv.it": "EQUtv",
    # ACI Sport TV
    "aci sport tv": "ACI Sport TV", "aci sport tv.it": "ACI Sport TV",
    "aci sport tv.it": "ACI Sport TV", "acisporttv.it": "ACI Sport TV",
    # Solo Calcio
    "solo calcio": "Solo Calcio", "si solo calcio.it": "Solo Calcio",
    "solocalcio.it.it": "Solo Calcio",
    # Marcopolo Travel TV
    "marcopolo travel tv": "Marcopolo Travel TV", "marcopolo travel.it": "Marcopolo Travel TV",
    # HGTV
    "hgtv": "HGTV", "hgtv hd": "HGTV", "hgtv hd.it": "HGTV", "hgtv it.it": "HGTV",
    "hgtv.hd..418.it": "HGTV", "home and garden tv.it": "HGTV",
    "hgtv  homeandgarden.it": "HGTV",
    # Euronews
    "euronews": "Euronews", "euronews.it": "Euronews",
    # Discovery Turbo
    "discovery turbo": "Discovery Turbo", "discovery turbo hd": "Discovery Turbo",
    "discovery turbo.it": "Discovery Turbo", "motor trend hd.it": "Discovery Turbo",
    "motor trend hd..419.it": "Discovery Turbo", "motor trend.it": "Discovery Turbo",
    "motortrend.it": "Discovery Turbo", "motor.trend.hd..419.it": "Discovery Turbo",
    # wedotv Big Stories
    "wedotv big stories": "wedotv Big Stories", "wedo big stories": "wedotv Big Stories",
    "wedo big stories.it": "wedotv Big Stories",
    # Radio Freccia TV
    "radio freccia tv": "Radio Freccia TV", "radio freccia": "Radio Freccia TV",
    "radiofreccia hd": "Radio Freccia TV", "radiofreccia hd.it": "Radio Freccia TV",
    "radiofreccia.hd.it": "Radio Freccia TV", "radiofreccia.it": "Radio Freccia TV",
    # Radio Monte Carlo TV
    "radio monte carlo tv": "Radio Monte Carlo TV",
    "radio monte carlo.it": "Radio Monte Carlo TV",
    "radiomontecarlo.it": "Radio Monte Carlo TV",
    # Virgin Radio TV
    "virgin radio tv": "Virgin Radio TV", "virgin radio.it": "Virgin Radio TV",
    "virginradio.it": "Virgin Radio TV",
    # France 24 English
    "france 24 english": "France 24 English", "france 24 english hd": "France 24 English",
    "france 24 english.it": "France 24 English", "france24english.it": "France 24 English",
    "france24en.it": "France 24 English",
    # BBC News
    "bbc news": "BBC News", "bbc world news": "BBC World News",
    "bbc news hd": "BBC News", "bbc world news.it": "BBC World News",
    # Al Jazeera English
    "al jazeera english": "Al Jazeera English",
    "al jazeera international": "Al Jazeera International",
    "al jazeera intl. hd": "Al Jazeera International",
    "aljazeera.it": "Al Jazeera", "aljazeera intl.it": "Al Jazeera International",
    # TRT World
    "trt world": "TRT World", "trtworld.it": "TRT World",
    # NHK World TV
    "nhk world tv": "NHK World TV", "nhk world tv hd": "NHK World TV",
    "nhk world.it": "NHK World TV", "nhk world tv hd.it": "NHK World TV",
    # France 24 en français
    "france 24 en français": "France 24 en français",
    "france 24 francais hd": "France 24 en français",
    "france 24 français": "France 24 en français",
    "france 24.it": "France 24 en français", "france24fr.it": "France 24 en français",
    "france24francais.it": "France 24 en français",
    # Al Jazeera
    "al jazeera": "Al Jazeera", "aljazeera.it": "Al Jazeera",
    # CNBC
    "cnbc": "CNBC", "cnbc hd.it": "CNBC", "cnbc.it": "CNBC",
    # Bloomberg Television
    "bloomberg television": "Bloomberg Television", "bloomberg": "Bloomberg Television",
    "bloomberg.it": "Bloomberg Television",
    # DW English
    "dw english": "DW English", "deutsche welle": "DW English",
    # Sky Uno
    "sky uno": "Sky Uno", "sky uno hd": "Sky Uno", "sky uno fhd": "Sky Uno",
    "sky uno full hd": "Sky Uno", "sky uno sd": "Sky Uno", "sky uno.it": "Sky Uno",
    "skyuno.it": "Sky Uno", "sky uno hd.it": "Sky Uno",
    # Sky Uno +
    "sky uno +": "Sky Uno +", "sky uno +1": "Sky Uno +",
    "sky uno + hd": "Sky Uno +", "sky uno +24": "Sky Uno +",
    "skyuno+.it": "Sky Uno +", "skyuno+1.it": "Sky Uno +",
    # Sky Atlantic
    "sky atlantic": "Sky Atlantic", "sky atlantic hd": "Sky Atlantic",
    "sky atlantic fhd": "Sky Atlantic", "sky atlantic full hd": "Sky Atlantic",
    "sky atlantic sd": "Sky Atlantic", "sky atlantic.it": "Sky Atlantic",
    "skyatlantic.it": "Sky Atlantic", "sky atlantic hd.it": "Sky Atlantic",
    "sky atlantic maratone.it": "Sky Atlantic",
    # Sky Serie
    "sky serie": "Sky Serie", "sky serie hd.it": "Sky Serie",
    "sky serie.it": "Sky Serie", "skyserie.it": "Sky Serie",
    "sky serie fhd": "Sky Serie", "sky serie full hd": "Sky Serie",
    # Sky Investigation
    "sky investigation": "Sky Investigation", "sky investigation.it": "Sky Investigation",
    "skyinvestigation.it": "Sky Investigation",
    # Sky Crime
    "sky crime": "Sky Crime", "sky crime hd": "Sky Crime",
    "sky crime.it": "Sky Crime", "skycrime.it": "Sky Crime",
    # History
    "history": "History", "history channel": "History", "history hd": "History",
    "history fhd": "History", "history full hd": "History", "history sd": "History",
    "history.it": "History", "history channel.it": "History",
    # Sky Documentaries
    "sky documentaries": "Sky Documentaries", "sky documentaries.it": "Sky Documentaries",
    "skydocumentaries.it": "Sky Documentaries", "sky documentaries hd.it": "Sky Documentaries",
    # Sky Adventure
    "sky adventure": "Sky Adventure", "sky adventure.it": "Sky Adventure",
    # Sky Nature
    "sky nature": "Sky Nature", "sky nature.it": "Sky Nature",
    "skynature.it": "Sky Nature", "sky nature hd.it": "Sky Nature",
    # Sky Classica
    "sky classica": "Sky Classica", "classica hd.it": "Sky Classica",
    "classica.it": "Sky Classica", "sky classica.it": "Sky Classica",
    # Comedy Central
    "comedy central": "Comedy Central", "comedy central hd": "Comedy Central",
    "comedy central fhd": "Comedy Central", "comedy central full hd": "Comedy Central",
    "comedy central sd": "Comedy Central", "comedy central.it": "Comedy Central",
    "comedycentral.it": "Comedy Central",
    # MTV
    "mtv": "MTV", "mtv hd": "MTV", "mtv hd.it": "MTV", "mtv.it": "MTV",
    "mtvmusic.it": "MTV", "mtv music.it": "MTV",
    # Gambero Rosso
    "gambero rosso": "Gambero Rosso", "gambero rosso hd": "Gambero Rosso",
    "gambero rosso fhd": "Gambero Rosso", "gambero rosso full hd": "Gambero Rosso",
    "gambero rosso sd": "Gambero Rosso", "gambero rosso.it": "Gambero Rosso",
    "gamberorosso.it": "Gambero Rosso", "gambero rosso hd.it": "Gambero Rosso",
    # Sky Sport 24
    "sky sport 24": "Sky Sport 24", "sky sport 24.it": "Sky Sport 24",
    "skysport24.it": "Sky Sport 24",
    # Sky Sport Uno
    "sky sport uno": "Sky Sport Uno", "sky sport": "Sky Sport Uno",
    "sky sport hd": "Sky Sport Uno", "sky sport fhd": "Sky Sport Uno",
    "sky sport full hd": "Sky Sport Uno", "sky sport sd": "Sky Sport Uno",
    "sky sport uno.it": "Sky Sport Uno", "skysportuno.it": "Sky Sport Uno",
    "sky sport 4k.it": "Sky Sport Uno", "skysport4k.it": "Sky Sport Uno",
    "sky sport.it": "Sky Sport Uno", "skysport.it": "Sky Sport Uno",
    # Sky Sport Calcio
    "sky sport calcio": "Sky Sport Calcio", "sky sport calcio.it": "Sky Sport Calcio",
    "skysportcalcio.it": "Sky Sport Calcio", "sky sport football": "Sky Sport Calcio",
    "sky sport football hd": "Sky Sport Calcio", "sky sport football fhd": "Sky Sport Calcio",
    # Rai Radio 2 Visual
    "rai radio 2 visual": "Rai Radio 2 Visual", "rai radio 2.it": "Rai Radio 2 Visual",
    "rairadio2.it": "Rai Radio 2 Visual",
    # Sky Sport Tennis
    "sky sport tennis": "Sky Sport Tennis", "sky sport tennis.it": "Sky Sport Tennis",
    "skysporttennis.it": "Sky Sport Tennis",
    # Sky Sport Arena
    "sky sport arena": "Sky Sport Arena", "sky sport arena hd": "Sky Sport Arena",
    "sky sport arena fhd": "Sky Sport Arena", "sky sport arena full hd": "Sky Sport Arena",
    "sky sport arena.it": "Sky Sport Arena", "skysportarena.it": "Sky Sport Arena",
    # Sky Sport Basket
    "sky sport basket": "Sky Sport Basket", "sky sport nba": "Sky Sport Basket",
    "sky sport nba hd": "Sky Sport Basket", "sky sport nba fhd": "Sky Sport Basket",
    "sky sport nba.it": "Sky Sport Basket", "skysportnba.it": "Sky Sport Basket",
    # Sky Sport Max
    "sky sport max": "Sky Sport Max", "sky sport max.it": "Sky Sport Max",
    "skysportmax.it": "Sky Sport Max",
    # Sky Sport F1
    "sky sport f1": "Sky Sport F1", "sky sport f1 hd": "Sky Sport F1",
    "sky sport f1 fhd": "Sky Sport F1", "sky sport f1 full hd": "Sky Sport F1",
    "sky sport f1.it": "Sky Sport F1", "skysportf1.it": "Sky Sport F1",
    "sky sport f1 hd.it": "Sky Sport F1",
    # Sky Sport MotoGP
    "sky sport motogp": "Sky Sport MotoGP", "sky sport motogp hd": "Sky Sport MotoGP",
    "sky sport motogp fhd": "Sky Sport MotoGP", "sky sport motogp.it": "Sky Sport MotoGP",
    "skysportmotogp.it": "Sky Sport MotoGP",
    # Sky Sport Golf
    "sky sport golf": "Sky Sport Golf", "sky sport golf.it": "Sky Sport Golf",
    "skysportgolf.it": "Sky Sport Golf",
    # Sky Sport Legend
    "sky sport legend": "Sky Sport Legend", "sky sport legend.it": "Sky Sport Legend",
    "skysportlegend.it": "Sky Sport Legend",
    # Sky Sport Mix
    "sky sport mix": "Sky Sport Mix", "sky sport mix.it": "Sky Sport Mix",
    # SuperTennis
    "supertennis": "SuperTennis", "supertennis hd": "SuperTennis",
    "supertennis hd.it": "SuperTennis", "supertennis.it": "SuperTennis",
    "super tennis hd.it": "SuperTennis",
    # Zona DAZN
    "zona dazn": "Zona DAZN", "dazn 1.it": "Zona DAZN", "dazn 1 hd": "Zona DAZN",
    "dazn1linear.it": "Zona DAZN", "zonadazn.it": "Zona DAZN", "zona dazn.it": "Zona DAZN",
    # Zona DAZN 2-5
    "zona dazn 2": "Zona DAZN 2", "zonadazn2.it": "Zona DAZN 2", "dazn 2 hd": "Zona DAZN 2",
    "zona dazn 3": "Zona DAZN 3", "zonadazn3.it": "Zona DAZN 3",
    "zona dazn 4": "Zona DAZN 4", "zonadazn4.it": "Zona DAZN 4",
    "zona dazn 5": "Zona DAZN 5", "zonadazn5.it": "Zona DAZN 5",
    # Horse TV
    "horse tv": "Horse TV", "horse tv hd": "Horse TV",
    "horse tv hd.it": "Horse TV", "horsetv.it": "Horse TV",
    # MyZen TV
    "myzen tv": "MyZen TV", "myzen tv.it": "MyZen TV",
    # Travel XP
    "travel xp": "Travel XP", "travelxp 4k.it": "Travel XP",
    # Milan TV
    "milan tv": "Milan TV", "milan tv.it": "Milan TV", "milanTV.it": "Milan TV",
    # Inter TV
    "inter tv": "Inter TV", "inter tv hd.it": "Inter TV",
    "inter tv.it": "Inter TV", "intertv.it": "Inter TV",
    # Caccia
    "caccia": "Caccia", "caccia & pesca": "Caccia", "caccia & pesca hd": "Caccia",
    "caccia e pesca.it": "Caccia", "caccia.it": "Caccia",
    "caccia &amp; pesca": "Caccia", "caccia &amp; pesca hd": "Caccia",
    # Pesca
    "pesca": "Pesca", "pesca & caccia": "Pesca", "pesca & caccia hd": "Pesca",
    "pesca e caccia.it": "Pesca", "pesca.it": "Pesca",
    # Sky Sport 251-261
    **{f"sky sport hd {i}.it": f"Sky Sport {250+i}" for i in range(1, 12)},
    **{f"sky sport {250+i}": f"Sky Sport {250+i}" for i in range(1, 12)},
    **{f"skysport{250+i}.it": f"Sky Sport {250+i}" for i in range(1, 12)},
    **{f"sky.sport..{250+i}.it": f"Sky Sport {250+i}" for i in range(1, 12)},
    # Sky Cinema
    "sky cinema uno": "Sky Cinema Uno", "sky cinema uno hd": "Sky Cinema Uno",
    "sky cinema uno fhd": "Sky Cinema Uno", "sky cinema uno full hd": "Sky Cinema Uno",
    "sky cinema uno sd": "Sky Cinema Uno", "sky cinema uno.it": "Sky Cinema Uno",
    "skyCinemaUno.it": "Sky Cinema Uno",
    "sky cinema due": "Sky Cinema Due", "sky cinema due hd": "Sky Cinema Due",
    "sky cinema due fhd": "Sky Cinema Due", "sky cinema due full hd": "Sky Cinema Due",
    "sky cinema due sd": "Sky Cinema Due", "sky cinema due.it": "Sky Cinema Due",
    "skycinemaDue.it": "Sky Cinema Due",
    "sky cinema collection": "Sky Cinema Collection",
    "sky cinema collection hd.it": "Sky Cinema Collection",
    "sky cinema collection.it": "Sky Cinema Collection",
    "sky cinema family": "Sky Cinema Family", "sky cinema family hd.it": "Sky Cinema Family",
    "sky cinema family.it": "Sky Cinema Family",
    "sky cinema action": "Sky Cinema Action", "sky cinema action hd.it": "Sky Cinema Action",
    "sky cinema action.it": "Sky Cinema Action",
    "sky cinema suspense": "Sky Cinema Suspense", "sky cinema suspence": "Sky Cinema Suspense",
    "sky cinema suspense hd.it": "Sky Cinema Suspense",
    "sky cinema comedy": "Sky Cinema Comedy", "sky cinema comedy hd.it": "Sky Cinema Comedy",
    "sky cinema comedy.it": "Sky Cinema Comedy",
    "sky cinema romance": "Sky Cinema Romance", "sky cinema romance hd.it": "Sky Cinema Romance",
    "sky cinema drama": "Sky Cinema Drama", "sky cinema drama hd.it": "Sky Cinema Drama",
    "sky cinema uno+24": "Sky Cinema Uno+24", "sky cinema uno +24": "Sky Cinema Uno+24",
    "sky cinema uno +1": "Sky Cinema Uno+24", "skyCinemaUno+24.it": "Sky Cinema Uno+24",
    "sky cinema due +24": "Sky Cinema Due +24", "sky cinema due +1": "Sky Cinema Due +24",
    "skycinemaDue+24.it": "Sky Cinema Due +24",
    "sky cinema cult": "Sky Cinema Cult", "sky cinema cult.it": "Sky Cinema Cult",
    # Sky TG24
    "sky tg24": "Sky TG24", "skytg24.it": "Sky TG24", "sky tg24.it": "Sky TG24",
    "sky tg24 primo piano": "Sky TG24 Primo Piano", "tg24primopiano.it": "Sky TG24 Primo Piano",
    "sky meteo 24": "Sky Meteo 24", "sky meteo24.it": "Sky Meteo 24",
    # Class CNBC
    "class cnbc": "Class CNBC", "class cnbc.it": "Class CNBC",
    "classcnbc.it": "Class CNBC", "class-cnbc.it": "Class CNBC",
    # TRM h24
    "trm h24": "TRM h24", "trm.h24.it": "TRM h24", "trmh24.it": "TRM h24",
    # Sky News
    "sky news": "Sky News", "sky news.it": "Sky News", "skynews.it": "Sky News",
    # CNN International
    "cnn international": "CNN International", "cnn intl.it": "CNN International",
    "cnnintl.it": "CNN International",
    # San Marino RTV
    "san marino rtv": "San Marino RTV", "san marino rtv.it": "San Marino RTV",
    "sanmarinortv.it": "San Marino RTV",
    # DeA Kids
    "dea kids": "DeA Kids", "deakids": "DeA Kids", "deakids.it": "DeA Kids",
    "dea kids hd": "DeA Kids", "dea kids.it": "DeA Kids",
    # Nick Jr.
    "nick jr.": "Nick Jr.", "nick jr": "Nick Jr.", "nick junior": "Nick Jr.",
    "nick jr.it": "Nick Jr.", "nick junior.it": "Nick Jr.", "nickjr.it": "Nick Jr.",
    # Nickelodeon
    "nickelodeon": "Nickelodeon", "nickelodeon hd": "Nickelodeon",
    "nickelodeon fhd": "Nickelodeon", "nickelodeon.it": "Nickelodeon",
    # Disney Jr.
    "disney jr.": "Disney Jr.", "disney junior": "Disney Jr.",
    # DeA Junior
    "dea junior": "DeA Junior", "deajunior": "DeA Junior", "deajunior.it": "DeA Junior",
    "dea junior hd": "DeA Junior", "dea junior.it": "DeA Junior",
    # Cartoon Network
    "cartoon network": "Cartoon Network", "cartoon network hd": "Cartoon Network",
    "cartoon network.it": "Cartoon Network", "cartoonnetwork.it": "Cartoon Network",
    # Boomerang
    "boomerang": "Boomerang", "boomerang hd": "Boomerang", "boomerang fhd": "Boomerang",
    "boomerang full hd": "Boomerang", "boomerang sd": "Boomerang", "boomerang.it": "Boomerang",
    # Deejay TV
    "deejay tv": "Deejay TV", "deejay tv.it": "Deejay TV", "deejaytv.it": "Deejay TV",
    # Radionorba TV
    "radionorba tv": "Radionorba TV", "radionorba tv.it": "Radionorba TV",
    "radionorbatv.it": "Radionorba TV",
    # DAZN 1-100
    **{f"dazn {i}": f"DAZN {i}" for i in range(1, 101)},
    **{f"dazn {i}.it": f"DAZN {i}" for i in range(1, 101)},
    **{f"dazn{i}.it": f"DAZN {i}" for i in range(1, 101)},
    # 7 Gold
    "7 gold": "7 Gold", "7 gold.it": "7 Gold", "italia 7 gold": "7 Gold",
    # Alma TV
    "alma tv": "Alma TV", "alma tv.it": "Alma TV", "almatv.it": "Alma TV",
    # Automoto
    "automoto": "Automoto", "automoto.it": "Automoto", "automoto hd": "Automoto",
    # BIKE Channel
    "bike channel": "BIKE Channel", "bike channel hd": "BIKE Channel",
    "bike.it": "BIKE Channel", "bike channel fhd": "BIKE Channel",
    # Blaze
    "blaze": "Blaze", "blaze hd": "Blaze", "blaze.it": "Blaze",
    # Donna TV
    "donna tv": "Donna TV", "donna tv.it": "Donna TV",
    # Fashion TV
    "fashion tv": "Fashion TV", "fashion tv.it": "Fashion TV",
    # Fox
    "fox": "Fox", "fox.it": "Fox",
    # Eurosport 1
    "eurosport 1": "Eurosport 1", "eurosport 1 hd": "Eurosport 1",
    "eurosport 1 fhd": "Eurosport 1", "eurosport.it": "Eurosport 1",
    "eurosport 1.it": "Eurosport 1", "eurosport italia.it": "Eurosport 1",
    # Eurosport 2
    "eurosport 2": "Eurosport 2", "eurosport 2 hd": "Eurosport 2",
    "eurosport 2 fhd": "Eurosport 2", "eurosport2.it": "Eurosport 2",
    "eurosport 2.it": "Eurosport 2",
    # National Geographic
    "national geographic": "National Geographic",
    "national geographic hd": "National Geographic",
    "nationalGeo.it": "National Geographic",
    # Nat Geo Wild
    "nat geo wild": "Nat Geo Wild", "nat geo wild hd": "Nat Geo Wild",
    "natgeowild.it": "Nat Geo Wild",
    # SuperTennis alias extra
    "super tennis hd": "SuperTennis",
    # Sportitalia
    "sportitalia": "Sportitalia", "sport italia": "Sportitalia",
    "sport italia.it": "Sportitalia", "sportitalia.it": "Sportitalia",
    # RSI LA1/LA2
    "rsi la1": "RSI LA1", "rsi la1.it": "RSI LA1", "rsi la1 hd": "RSI LA1",
    "rsi la2": "RSI LA2", "rsi la2.it": "RSI LA2", "rsi la2 hd": "RSI LA2",
    # Radio 105
    "radio 105": "Radio 105", "radio 105 tv": "Radio 105", "radio 105.it": "Radio 105",
    # RMC
    "rmc": "RMC", "rmc.it": "RMC",
    # Roma TV
    "roma tv": "Roma TV", "romatv.it": "Roma TV",
    # Top Calcio 24
    "top calcio 24": "Top Calcio 24", "top calcio 24.it": "Top Calcio 24",
    # Euronews English
    "euronews english": "Euronews English",
    # VH1
    "vh1": "VH1", "vh1.it": "VH1",
    # LaEffe
    "laeffe": "LaEffe", "laeffe hd": "LaEffe",
    # Primafila 1-18
    **{f"primafila {i}": f"Primafila {i}" for i in range(1, 19)},
    **{f"prima fila {i}": f"Primafila {i}" for i in range(1, 19)},
    **{f"primafila{i}.it": f"Primafila {i}" for i in range(1, 19)},
    # Sky Arte (senza id separato nel riferimento, ma presente)
    "sky arte": "Sky Arte", "sky arte hd": "Sky Arte", "sky arte hd.it": "Sky Arte",
    "sky arte.it": "Sky Arte", "skyarte.it": "Sky Arte",
    # i24news
    "i24news": "i24news", "i24news.it": "i24news",
    # TG NORBA 24
    "tg norba 24": "TG NORBA 24", "tg norba 24.it": "TG NORBA 24",
    "tgnorba24.it": "TG NORBA 24",
    # Serie A squadre
    "atalanta.seriea": "ATALANTA", "bologna.seriea": "BOLOGNA",
    "cagliari.seriea": "CAGLIARI", "como.seriea": "COMO",
    "cremonese.seriea": "CREMONESE", "fiorentina.seriea": "FIORENTINA",
    "genoa.seriea": "GENOA", "hellasverona.seriea": "HELLAS VERONA",
    "inter.seriea": "INTER", "juventus.seriea": "JUVENTUS",
    "lazio.seriea": "LAZIO", "lecce.seriea": "LECCE",
    "milan.seriea": "MILAN", "napoli.seriea": "NAPOLI",
    "parma.seriea": "PARMA", "pisa.seriea": "PISA",
    "roma.seriea": "ROMA", "sassuolo.seriea": "SASSUOLO",
    "torino.seriea": "TORINO", "udinese.seriea": "UDINESE",
}

# Build a quick reverse-lookup: normalise alias → canonical id
def _norm(s: str) -> str:
    return s.strip().lower()

ALIAS_MAP: dict[str, str] = {_norm(k): v for k, v in CHANNEL_ALIASES.items()}


def resolve_channel_id(raw_id: str) -> str:
    """
    Tenta di risolvere raw_id al canonical channel id.
    Prova prima la stringa pulita, poi con .it rimosso, poi senza suffisso HD/FHD/SD.
    """
    key = _norm(raw_id)
    if key in ALIAS_MAP:
        return ALIAS_MAP[key]
    # prova senza trailing .it
    key2 = key.rstrip(".it").rstrip(".")
    if key2 in ALIAS_MAP:
        return ALIAS_MAP[key2]
    # prova rimuovendo suffissi comuni
    for suffix in (" hd", " fhd", " sd", " full hd", ".hd", ".sd"):
        if key.endswith(suffix):
            k3 = key[: -len(suffix)].strip(" .")
            if k3 in ALIAS_MAP:
                return ALIAS_MAP[k3]
    # fallback: restituisce raw_id invariato
    return raw_id


def score_programme(prog_elem) -> int:
    """
    Assegna un punteggio a un elemento <programme> in base alla ricchezza delle info.
    Più info → punteggio più alto.
    """
    score = 0
    for child in prog_elem:
        score += 1
        if child.text and child.text.strip():
            score += 2
        if child.tag == "desc":
            score += 5
        if child.tag == "episode-num":
            score += 3
        if child.tag in ("icon", "category", "rating", "star-rating"):
            score += 2
    return score


def parse_gz_epg(filepath: str) -> tuple[dict, dict]:
    """
    Parsa un file EPG .gz e restituisce:
      - channels: dict  canonical_id → <channel> element
      - programmes: dict  canonical_id → list of <programme> elements
    """
    channels: dict[str, object] = {}
    programmes: dict[str, list] = defaultdict(list)

    try:
        with gzip.open(filepath, "rb") as f:
            data = f.read()
    except Exception as e:
        print(f"  [WARN] Impossibile aprire {filepath}: {e}", file=sys.stderr)
        return channels, programmes

    try:
        root = etree.fromstring(data)
    except Exception as e:
        print(f"  [WARN] XML non valido in {filepath}: {e}", file=sys.stderr)
        return channels, programmes

    # canali
    for ch in root.findall("channel"):
        raw_id = ch.get("id", "")
        cid = resolve_channel_id(raw_id)
        ch.set("id", cid)
        if cid not in channels:
            channels[cid] = ch

    # programmi
    for prog in root.findall("programme"):
        raw_id = prog.get("channel", "")
        cid = resolve_channel_id(raw_id)
        prog.set("channel", cid)
        programmes[cid].append(prog)

    return channels, programmes


def merge_epg(epg_dir: str, output_path: str) -> None:
    gz_files = sorted(glob.glob(os.path.join(epg_dir, "*.gz")))
    # Escludiamo il file di output se già esiste nella stessa cartella
    gz_files = [f for f in gz_files if os.path.basename(f) != os.path.basename(output_path)]

    if not gz_files:
        print("Nessun file .gz trovato in", epg_dir, file=sys.stderr)
        sys.exit(1)

    print(f"Trovati {len(gz_files)} file EPG sorgente.")

    # Strutture aggregate
    all_channels: dict[str, object] = {}
    # per ogni canale: lista di (score_totale, [elementi programme])
    channel_source_programmes: dict[str, list[tuple[int, list]]] = defaultdict(list)

    for gz_path in gz_files:
        fname = os.path.basename(gz_path)
        print(f"  Parsing: {fname} ...", end=" ")
        chs, progs = parse_gz_epg(gz_path)
        print(f"{len(chs)} canali, {sum(len(v) for v in progs.values())} programmi")

        for cid, ch_elem in chs.items():
            if cid not in all_channels:
                all_channels[cid] = ch_elem

        for cid, prog_list in progs.items():
            if prog_list:
                total_score = sum(score_programme(p) for p in prog_list)
                channel_source_programmes[cid].append((total_score, prog_list))

    # Per ogni canale, scegli la fonte con punteggio totale più alto
    best_programmes: dict[str, list] = {}
    for cid, sources in channel_source_programmes.items():
        best_score, best_list = max(sources, key=lambda x: x[0])
        best_programmes[cid] = best_list

    # Costruisci l'XML finale
    tv_root = etree.Element("tv")
    tv_root.set("generator-info-name", "merge_epg.py")

    # Prima tutti i <channel>
    for cid in sorted(all_channels.keys()):
        ch = all_channels[cid]
        ch.set("id", cid)
        # assicura display-name
        if ch.find("display-name") is None:
            dn = etree.SubElement(ch, "display-name")
            dn.text = cid
        tv_root.append(ch)

    # Poi tutti i <programme> ordinati per start
    all_progs = []
    for cid, progs in best_programmes.items():
        all_progs.extend(progs)

    all_progs.sort(key=lambda p: p.get("start", ""))
    for prog in all_progs:
        tv_root.append(prog)

    total_ch = len(all_channels)
    total_prog = len(all_progs)
    print(f"\nEPG unificato: {total_ch} canali, {total_prog} programmi totali.")

    # Scrivi su gz
    xml_bytes = etree.tostring(tv_root, xml_declaration=True, encoding="UTF-8", pretty_print=False)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with gzip.open(output_path, "wb", compresslevel=6) as f:
        f.write(xml_bytes)

    size_kb = os.path.getsize(output_path) // 1024
    print(f"Output scritto: {output_path} ({size_kb} KB)")


if __name__ == "__main__":
    epg_dir = sys.argv[1] if len(sys.argv) > 1 else "epg"
    output = sys.argv[2] if len(sys.argv) > 2 else "epg/merged_epg.xml.gz"
    merge_epg(epg_dir, output)
