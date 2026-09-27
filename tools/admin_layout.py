"""Split states along 2000 first-level administrative divisions (tools/data/README.md, "Administrative layout").

Every land province is assigned to the division covering most of its area:

  1. Map pixels are georeferenced with a locally-weighted affine fit through
     dense anchors: every victory point whose name matches a GeoNames city
     (tools/cache/geo/cities15000.zip) near the coarse estimate from province_geo.
  2. ~40 pixels per province are projected to lat/lon and tested against Natural
     Earth admin-1 polygons (tools/cache/geo/ne_admin1.geojson); the division
     with most samples wins. Countries whose first level is stored in the
     `region` field, France's 22 regions of 2000 and later-created divisions are
     normalised by DIVISION_RULES below.

Then, for each state, the division holding most of its population keeps the
state's ID. Parts in other divisions move to an adjacent existing state of that
division, or form a new state (one per owner and division). Provinces already
placed by curated splits (data/splits/*.csv without the admin_ prefix) are left
alone, as are parts that fall in another country's divisions (map noise at
international borders, which are curated separately).

Output (regenerated each run, then applied by apply_state_data.py):
  tools/data/splits/admin_<region>.csv   province moves
  tools/data/states/admin_<region>.csv   rows for the new states
  tools/data/states/<region>.csv          parent rows' manpower (and category if the
                                          state lost over a quarter of its people)
Population is split by GeoNames city populations plus a rural share by area.

Usage:
    python tools/admin_layout.py --report          # dry run: what would change
    python tools/admin_layout.py                   # write the data files
    python tools/admin_layout.py --check-geo       # accuracy of the pixel->lat/lon fit
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

import province_geo
import states as st

TOOLS = Path(__file__).resolve().parent
DATA = TOOLS / "data"
GEO = TOOLS / "cache" / "geo"
SAMPLES_PER_PROVINCE = 40
K = 10

# Countries whose Natural Earth units are second level (or too fine for the hybrid layout),
# with the level we use in `region`
REGION_LEVEL = {"GBR", "SVN", "LVA", "PHL", "UGA", "ITA", "MKD", "AZE", "MLT", "BFA", "GIN", "MWI",
                "SRB", "LKA", "KEN", "ESP", "IRL", "THA", "HUN", "PRT", "BIH"}
REGION_RENAME = {"Repuplika Srpska": "Republika Srpska",
                 "Federacija Bosna i Hercegovina": "Federation of Bosnia and Herzegovina",
                 "Norte, Centro": "Centro"}

# Hybrid layout: countries with many small first-level units use an official coarser grouping.
# Keys are Natural Earth `name` values; unlisted units keep their own name.
COARSE = {
    "TUR": {  # NUTS-1 statistical regions
        "Istanbul": "Istanbul",
        "West Marmara": "Tekirdag Edirne Kirklareli Balikesir Çanakkale",
        "Aegean": "Izmir Aydin Denizli Mugla Manisa Afyonkarahisar Kütahya Usak",
        "East Marmara": "Bursa Eskisehir Bilecik Kocaeli Sakarya Düzce Bolu Yalova",
        "West Anatolia": "Ankara Konya Karaman",
        "Mediterranean": "Antalya Isparta Burdur Adana Mersin Hatay K._Maras Osmaniye",
        "Central Anatolia": "Kinkkale Aksaray Nigde Nevsehir Kirsehir Kayseri Sivas Yozgat",
        "West Black Sea": "Zinguldak Karabük Bartın Kastamonu Çankiri Sinop Samsun Tokat Çorum Amasya",
        "East Black Sea": "Trabzon Ordu Giresun Rize Artvin Gümüshane",
        "Northeast Anatolia": "Erzurum Erzincan Bayburt Agri Kars Iğdir Ardahan",
        "Central East Anatolia": "Malatya Elazig Bingöl Tunceli Van Mus Bitlis Hakkari",
        "Southeast Anatolia": "Gaziantep Adiyaman Kilis Sanliurfa Diyarbakir Mardin Batman Sirnak Siirt",
    },
    "ROU": {  # development regions
        "Nord-Vest": "Bihor Bistrita-Nasaud Cluj Maramures Satu_Mare Salaj",
        "Centru": "Alba Brasov Covasna Harghita Mures Sibiu",
        "Nord-Est": "Bacau Botosani Iasi Neamt Suceava Vaslui",
        "Sud-Est": "Braila Buzau Constanta Galati Tulcea Vrancea",
        "Sud-Muntenia": "Arges Calarasi Dâmbovita Giurgiu Ialomita Prahova Teleorman",
        "Bucuresti-Ilfov": "Bucharest Ilfov",
        "Sud-Vest Oltenia": "Dolj Gorj Mehedinti Olt Vâlcea",
        "Vest": "Arad Caras-Severin Hunedoara Timis",
    },
    "BGR": {  # planning regions
        "Northwest": "Vidin Montana Vratsa Pleven Lovech",
        "North Central": "Veliko_Tarnovo Gabrovo Ruse Razgrad Silistra",
        "Northeast": "Varna Dobrich Shumen Targovishte",
        "Southeast": "Burgas Sliven Yambol Stara_Zagora",
        "Southwest": "Grad_Sofiya Sofia Blagoevgrad Pernik Kyustendil",
        "South Central": "Plovdiv Haskovo Pazardzhik Smolyan Kardzhali",
    },
    "MNG": {
        "Western Mongolia": "Bayan-Ölgiy Govi-Altay Dzavhan Hovd Uvs",
        "Khangai": "Arhangay Bayanhongor Bulgan Orhon Övörhangay Hövsgöl",
        "Central Mongolia": "Govĭ-Sümber Darhan-Uul Dornogovi Dundgovi Ömnögovi Selenge Töv",
        "Eastern Mongolia": "Dornod Hentiy Sühbaatar",
        "Ulaanbaatar": "Ulaanbaatar",
    },
    "FIN": {  # provinces (lääni) 1997-2009
        "Southern Finland": "Uusimaa Kymenlaakso South_Karelia Päijät-Häme Tavastia_Proper",
        "Western Finland": "Finland_Proper Satakunta Pirkanmaa Central_Finland Southern_Ostrobothnia "
                           "Ostrobothnia Central_Ostrobothnia",
        "Eastern Finland": "Southern_Savonia Northern_Savonia North_Karelia",
        "Oulu": "Northern_Ostrobothnia Kainuu",
        "Lapland": "Lapland",
    },
    "MDG": {  # the six provinces of 2000
        "Antananarivo": "Analamanga Bongolava Itasy Vakinankaratra",
        "Antsiranana": "Diana Sava",
        "Fianarantsoa": "Amoron'i_Mania Haute_Matsiatra Ihorombe Vatovavy-Fitovinany Atsimo-Atsinanana",
        "Mahajanga": "Betsiboka Boeny Melaky Sofia",
        "Toamasina": "Alaotra-Mangoro Analanjirofo Atsinanana",
        "Toliara": "Androy Anosy Atsimo-Andrefana Menabe",
    },
    "LBY": {  # historic regions
        "Tripolitania": "Al_Jifarah Al_Marqab An_Nuqat_al_Khams Az_Zawiyah Misratah Mizdah Surt "
                        "Tajura'_wa_an_Nawahi_al_Arba Ghadamis",
        "Cyrenaica": "Ajdabiya Al_Butnan Al_Jabal_al_Akhdar Al_Marj Al_Qubbah Benghazi Al_Kufrah",
        "Fezzan": "Al_Jufrah Ash_Shati' Ghat Murzuq Sabha Wadi_al_Hayaa",
    },
    "LAO": {
        "Northern Laos": "Bokeo Houaphan Louang_Namtha Louangphrabang Oudômxai Phôngsali Xaignabouri Xiangkhoang",
        "Central Laos": "Vientiane Vientiane_[prefecture] Bolikhamxai Khammouan Savannakhét",
        "Southern Laos": "Attapu Champasak Saravan Xékong",
    },
    "KHM": {  # NIS statistical zones
        "Phnom Penh": "Phnom_Penh",
        "Plain": "Kândal Kâmpóng_Cham Prey_Vêng Svay_Rieng Takêv",
        "Tonle Sap": "Bântéay_Méanchey Batdâmbâng Kâmpóng_Chhnang Kâmpóng_Thum Pouthisat Siemréab "
                     "Otdar_Mean_Chey Krong_Pailin",
        "Coastal": "Kâmpôt Kaôh_Kong Krong_Preah_Sihanouk Kep Kâmpóng_Spœ",
        "Plateau and Mountain": "Krâchéh Môndól_Kiri Preah_Vihéar Rôtânôkiri Stœng_Trêng",
    },
    "DZA": {  # SNAT programme regions
        "Nord-Centre": "Alger Blida Boumerdès Tipaza Bouira Médéa Tizi_Ouzou Béjaïa Chlef Aïn_Defla",
        "Nord-Est": "Annaba Constantine Skikda Jijel Mila Souk_Ahras El_Tarf Guelma",
        "Nord-Ouest": "Oran Tlemcen Mostaganem Aïn_Témouchent Relizane Sidi_Bel_Abbès Mascara",
        "Hauts Plateaux-Centre": "Djelfa Laghouat M'Sila",
        "Hauts Plateaux-Est": "Sétif Batna Khenchela Bordj_Bou_Arréridj Oum_el_Bouaghi Tébessa",
        "Hauts Plateaux-Ouest": "Tiaret Saïda Tissemsilt Naâma El_Bayadh",
        "Sud-Est": "Biskra El_Oued Ghardaïa Ouargla",
        "Sud-Ouest": "Béchar Tindouf Adrar",
        "Grand Sud": "Illizi Tamanghasset",
    },
    "TUN": {
        "Grand Tunis": "Tunis Ben_Arous_(Tunis_Sud) Manubah",
        "Nord-Est": "Nabeul Zaghouan Bizerte",
        "Nord-Ouest": "Béja Jendouba Le_Kef Siliana",
        "Centre-Est": "Sousse Monastir Mahdia Sfax",
        "Centre-Ouest": "Kairouan Kassérine Sidi_Bou_Zid",
        "Sud-Est": "Gabès Médenine Tataouine",
        "Sud-Ouest": "Gafsa Tozeur Kebili",
    },
    "EGY": {  # economic regions
        "Greater Cairo": "Al_Qahirah Al_Jizah Al_Qalyubiyah",
        "Alexandria": "Al_Iskandariyah Al_Buhayrah Matruh",
        "Delta": "Al_Minufiyah Al_Gharbiyah Kafr_ash_Shaykh Ad_Daqahliyah Dumyat",
        "Suez Canal": "Bur_Sa`id Al_Isma`iliyah As_Suways Ash_Sharqiyah Shamal_Sina' Janub_Sina'",
        "North Upper Egypt": "Bani_Suwayf Al_Fayyum Al_Minya",
        "Asyut": "Asyut Al_Wadi_at_Jadid",
        "South Upper Egypt": "Suhaj Qina Luxor Aswan Al_Bahr_al_Ahmar",
    },
    "HRV": {  # NUTS-2 regions 2007-2012
        "Northwest Croatia": "Grad_Zagreb Zagrebacka Krapinsko-Zagorska Varaždinska Koprivničko-Križevačka Medimurska",
        "Central and Eastern Croatia": "Bjelovarsko-bilogorska Viroviticko-Podravska Brodsko-Posavska "
                                       "Osjecko-Baranjska Vukovarsko-Srijemska Karlovacka Sisacko-Moslavacka",
        "Adriatic Croatia": "Primorsko-Goranska Licko-Senjska Zadarska Šibensko-Kninska Splitsko-Dalmatinska "
                            "Istarska Dubrovacko-Neretvanska",
    },
    "VNM": {  # socio-economic regions (NE's region field is incomplete)
        "Northeast": "Bắc_Giang Cao_Bằng Hà_Giang Lai_Chau Lào_Cai Lạng_Sơn Phú_Thọ Quảng_Ninh Thái_Nguyên "
                     "Tuyên_Quang Yên_Bái Đông_Bắc",
        "Northwest": "Hòa_Bình Son_La Điện_Biên",
        "Red River Delta": "Ha_Noi Bắc_Ninh Hà_Nam Hải_Dương Hải_Phòng Nam_Định Ninh_Bình Thái_Bình Vĩnh_Phúc "
                           "Đồng_Bằng_Sông_Hồng",
        "North Central Coast": "Ha_Tinh Nghệ_An Quảng_Bình Quảng_Trị Thanh_Hóa Thừa_Thiên_-_Huế",
        "South Central Coast": "Bình_Định Gia_Lai Khánh_Hòa Kon_Tum Phú_Yên Quàng_Nam Quảng_Ngãi Đà_Nẵng",
        "Central Highlands": "Lâm_Đồng Đắk_Lắk Đắk_Nông",
        "Southeast": "Bà_Rịa_-_Vũng_Tàu Bình_Dương Bình_Phước Bình_Thuận Hồ_Chí_Minh_city Ninh_Thuận Tây_Ninh Đông_Nam_Bộ",
        "Mekong Delta": "An_Giang Bạc_Liêu Bến_Tre Can_Tho Cà_Mau Hau_Giang Kiên_Giang Long_An Sóc_Trăng "
                        "Tiền_Giang Trà_Vinh Vĩnh_Long Ðong_Tháp",
    },
}
COARSE.update({
    "SRB": {  # statistical regions (Kosovo and Montenegro are SINGLE_UNIT)
        "Belgrade": "Grad_Beograd",
        "Vojvodina": "Južno-Backi Južno-Banatski Severno-Backi Severno-Banatski Srednje-Banatski Sremski Zapadno-Backi",
        "Šumadija and Western Serbia": "Kolubarski Macvanski Moravicki Pomoravski Raški Zlatiborski Šumadijski",
        "Southern and Eastern Serbia": "Borski Branicevski Jablanicki Nišavski Pcinjski Pirotski Podunavski "
                                       "Toplicki Zajecarski",
    },
    "EST": {
        "Northern Estonia": "Harju", "Northeastern Estonia": "Ida-Viru",
        "Western Estonia": "Hiiu Lääne Pärnu Saare", "Central Estonia": "Järva Lääne-Viru Rapla",
        "Southern Estonia": "Jõgeva Põlva Tartu Valga Viljandi Võru",
    },
    "ALB": {
        "Northern Albania": "Dibër Durrës Kukës Lezhë Shkodër", "Central Albania": "Elbasan Tiranë",
        "Southern Albania": "Berat Fier Gjirokastër Korçë Vlorë",
    },
    "MDA": {  # development regions
        "Chișinău": "Chişinău",
        "Northern Moldova": "Briceni Bălţi Donduseni Drochia Edineţ Floreşti Făleşti Glodeni Ocniţa Rîşcani "
                            "Soroca Sîngerei",
        "Central Moldova": "Anenii_Noi Criuleni Călărași Hîncesti Ialoveni Nisporeni Orhei Rezina Străşeni "
                           "Teleneşti Ungheni Şoldăneşti",
        "Southern Moldova": "Basarabeasca Cahul Cantemir Causeni Cimişlia Leova Taraclia Ștefan_Vodă",
        "Gagauzia": "Comrat",
        "Transnistria": "Transnistria Bender Camenca Grigoriopol Stîngă_Nistrului",
    },
    "TZA": {  # zones
        "Northern Zone": "Arusha Kilimanjaro Manyara Tanga",
        "Lake Zone": "Kagera Mwanza Mara Geita Simiyu Shinyanga",
        "Western Zone": "Kigoma Tabora Katavi", "Central Zone": "Dodoma Singida",
        "Southern Highlands": "Iringa Mbeya Njombe Rukwa", "Southern Zone": "Lindi Mtwara Ruvuma",
        "Eastern Zone": "Dar-Es-Salaam Pwani Morogoro",
        "Zanzibar": "Kaskazini-Pemba Kusini-Pemba Kaskazini-Unguja Zanzibar_South_and_Central Zanzibar_West",
    },
})
COARSE_OF = {a3: {n.replace("_", " "): g for g, names in groups.items() for n in names.split()}
             for a3, groups in COARSE.items()}
REGION_LEVEL.discard("SRB")  # handled by COARSE
# groupings applied on top of REGION_LEVEL `region` values
REGION_GROUP = {
    "SVN": {r: "Western Slovenia" for r in ("Osrednjeslovenska", "Gorenjska", "Goriška", "Obalno-kraška",
                                           "Notranjsko-kraška")},
    "MKD": {"Greater Skopje": "Skopje"},
}
# Whole country is one division (a constituent republic / province of a federation in 2000)
SINGLE_UNIT = {"MNE": "Montenegro", "KOS": "Kosovo", "HKG": "Hong Kong", "MAC": "Macau"}
# Divisions created after 1 Jan 2000, folded back into their 2000 parent (by NE name)
MERGE_2000 = {
    "IND": {"Telangana": "Andhra Pradesh", "Jharkhand": "Bihar", "Chhattisgarh": "Madhya Pradesh",
            "Uttarakhand": "Uttar Pradesh", "Ladakh": "Jammu and Kashmir"},
    "CHL": {"Arica y Parinacota": "Tarapacá", "Los Ríos": "Los Lagos", "Ñuble": "Bío-Bío"},
    "IRN": {"North Khorasan": "Khorasan", "South Khorasan": "Khorasan", "Razavi Khorasan": "Khorasan",
            "Alborz": "Tehran"},
    "IDN": {"Banten": "Jawa Barat", "Bangka-Belitung": "Sumatera Selatan", "Gorontalo": "Sulawesi Utara",
            "Kepulauan Riau": "Riau", "Sulawesi Barat": "Sulawesi Selatan", "Papua Barat": "Papua",
            "Kalimantan Utara": "Kalimantan Timur", "Papua Barat Daya": "Papua", "Papua Selatan": "Papua",
            "Papua Tengah": "Papua", "Papua Pegunungan": "Papua"},
}
# France: départements -> the 22 metropolitan regions of 2000
FRA_2000 = {
    "Alsace": "67 68", "Aquitaine": "24 33 40 47 64", "Auvergne": "03 15 43 63",
    "Basse-Normandie": "14 50 61", "Bourgogne": "21 58 71 89", "Bretagne": "22 29 35 56",
    "Centre": "18 28 36 37 41 45", "Champagne-Ardenne": "08 10 51 52", "Corse": "2A 2B",
    "Franche-Comté": "25 39 70 90", "Haute-Normandie": "27 76",
    "Île-de-France": "75 77 78 91 92 93 94 95", "Languedoc-Roussillon": "11 30 34 48 66",
    "Limousin": "19 23 87", "Lorraine": "54 55 57 88", "Midi-Pyrénées": "09 12 31 32 46 65 81 82",
    "Nord-Pas-de-Calais": "59 62", "Pays de la Loire": "44 49 53 72 85", "Picardie": "02 60 80",
    "Poitou-Charentes": "16 17 79 86", "Provence-Alpes-Côte d'Azur": "04 05 06 13 83 84",
    "Rhône-Alpes": "01 07 26 38 42 69 73 74",
}
FRA_DEPT = {d: r for r, ds in FRA_2000.items() for d in ds.split()}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]+", " ", s).strip()


def slug(s: str) -> str:
    return re.sub(r"_+", "_", norm(s).replace(" ", "_")).strip("_")[:40] or "x"


# --- geo reference ---------------------------------------------------------------

def load_cities() -> list[tuple[str, float, float, int, str]]:
    """(name, lat, lon, population, country) with every alternate name listed separately."""
    out = []
    with zipfile.ZipFile(GEO / "cities15000.zip") as z:
        with z.open("cities15000.txt") as f:
            for line in io.TextIOWrapper(f, encoding="utf-8"):
                c = line.rstrip("\n").split("\t")
                lat, lon, pop = float(c[4]), float(c[5]), int(c[14] or 0)
                names = {c[1], c[2]} | {a for a in c[3].split(",") if a and len(a) < 40}
                for n in names:
                    out.append((norm(n), lat, lon, pop, c[8]))
    return out


def haversine(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (np.sin((lat2 - lat1) * p / 2) ** 2
         + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * np.arcsin(np.sqrt(np.minimum(a, 1)))


class Georef:
    """Pixel (x, y) -> (lat, lon) through dense anchors, with horizontal wrap."""

    def __init__(self, anchors: np.ndarray, width: int):
        self.width = width
        shifted = [anchors]
        for dx, dlon in ((width, 360.0), (-width, -360.0)):
            a = anchors.copy()
            a[:, 0] += dx
            a[:, 3] += dlon
            shifted.append(a)
        self.a = np.vstack(shifted)
        self.tree = cKDTree(self.a[:, :2])

    def __call__(self, xy: np.ndarray, exclude_self: bool = False) -> np.ndarray:
        k = K + 1 if exclude_self else K
        d, idx = self.tree.query(xy, k=k)
        if exclude_self:
            d, idx = d[:, 1:], idx[:, 1:]
        w = 1.0 / np.maximum(d, 1.0) ** 2
        A = np.stack([np.ones_like(d), self.a[idx, 0] - xy[:, :1], self.a[idx, 1] - xy[:, 1:2]], axis=2)
        Aw = A * np.sqrt(w)[..., None]
        AtA = np.einsum("nki,nkj->nij", Aw, Aw) + np.diag([0.0, 1e-3, 1e-3])
        out = np.empty((len(xy), 2))
        for col, j in ((0, 2), (1, 3)):
            b = np.einsum("nki,nk->ni", Aw, self.a[idx, j] * np.sqrt(w))
            out[:, col] = np.linalg.solve(AtA, b[..., None])[:, 0, 0]
        out[:, 1] = (out[:, 1] + 180) % 360 - 180
        return out


def build_georef(hoi4: Path, table: dict[int, dict], verbose: bool = False) -> Georef:
    names = province_geo.vp_names(
        hoi4 / "localisation" / "english" / "victory_points_l_english.yml",
        st.REPO_ROOT / "localisation" / "replace" / "tnd_victory_points_l_english.yml",
    )
    cities = load_cities()
    by_name: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
    for n, lat, lon, pop, _ in cities:
        by_name[n].append((lat, lon, pop))
    anchors = []
    for pid, name in names.items():
        p = table.get(pid)
        if not p or p.get("type") != "land" or "lat" not in p:
            continue
        cands = by_name.get(norm(name))
        if not cands:
            continue
        c = np.array(cands)
        dist = haversine(p["lat"], p["lon"], c[:, 0], c[:, 1])
        i = int(np.argmin(dist))
        if dist[i] < 300:
            anchors.append((p["x"], p["y"], c[i, 0], c[i, 1]))
    a = np.array(anchors)
    width = int(max(p["x"] for p in table.values())) + 2
    # drop anchors that disagree with their neighbours (wrong same-name city)
    for _ in range(3):
        g = Georef(a, width)
        pred = g(a[:, :2], exclude_self=True)
        err = haversine(pred[:, 0], pred[:, 1], a[:, 2], a[:, 3])
        keep = err < max(120.0, float(np.percentile(err, 95)))
        if keep.all():
            break
        a = a[keep]
    g = Georef(a, width)
    if verbose:
        pred = g(a[:, :2], exclude_self=True)
        err = haversine(pred[:, 0], pred[:, 1], a[:, 2], a[:, 3])
        print(f"{len(a)} anchors; leave-one-out error: median {np.median(err):.0f} km, "
              f"90% {np.percentile(err, 90):.0f} km, max {err.max():.0f} km")
    return g


# --- divisions ---------------------------------------------------------------------

def load_divisions():
    """Shapely geometries with (country a3, division name) keys, normalised to 2000 first level."""
    import shapely
    from shapely.geometry import shape

    gj = json.loads((GEO / "ne_admin1.geojson").read_text(encoding="utf-8"))
    geoms, keys, wikidata = [], [], []
    for f in gj["features"]:
        p = f["properties"]
        a3 = p["adm0_a3"]
        ne_name = p.get("name") or "?"
        name = p.get("name_en") or ne_name or p.get("gn_name") or "?"
        if a3 in SINGLE_UNIT:
            name = SINGLE_UNIT[a3]
        elif a3 in COARSE_OF and ne_name in COARSE_OF[a3]:
            name = COARSE_OF[a3][ne_name]
        elif a3 == "FRA" and (p.get("iso_3166_2") or "").startswith("FR-"):
            code = p["iso_3166_2"][3:]
            name = FRA_DEPT.get(code, p.get("region") or name)
        elif a3 in REGION_LEVEL and p.get("region"):
            name = REGION_RENAME.get(p["region"], p["region"])
            if a3 in REGION_GROUP:
                name = REGION_GROUP[a3].get(name, "Eastern Slovenia" if a3 == "SVN" else name)
        else:
            parent = MERGE_2000.get(a3, {}).get(ne_name)
            if parent:
                name = parent
        geoms.append(shape(f["geometry"]))
        keys.append((a3, name))
        wikidata.append(p.get("wikidataid") or "")
    return shapely.STRtree(geoms), geoms, keys, wikidata


def province_divisions(hoi4: Path, table: dict[int, dict], georef: Georef, land: set[int]):
    """province -> (division key, share of samples), sampled from the province bitmap."""
    import shapely

    idmap = province_geo.province_id_map(hoi4)
    rng = np.random.default_rng(2000)
    flat = idmap.ravel()
    order = np.argsort(flat, kind="stable")
    sorted_ids = flat[order]
    starts = np.searchsorted(sorted_ids, np.arange(sorted_ids.max() + 2))
    h, w = idmap.shape
    pts, owners = [], []
    for pid in land:
        s, e = starts[pid], starts[pid + 1]
        if e <= s:
            continue
        pix = order[s:e]
        if len(pix) > SAMPLES_PER_PROVINCE:
            pix = rng.choice(pix, SAMPLES_PER_PROVINCE, replace=False)
        ys, xs = np.divmod(pix, w)
        pts.append(np.column_stack([xs, ys]).astype(float))
        owners.append(np.full(len(pix), pid))
    xy = np.vstack(pts)
    pid_of = np.concatenate(owners)
    ll = georef(xy)
    tree, geoms, keys, _ = load_divisions()
    points = shapely.points(ll[:, 1], ll[:, 0])
    hit = np.full(len(points), -1)
    pi, gi = tree.query(points, predicate="within")
    hit[pi] = gi
    missing = np.where(hit < 0)[0]
    if len(missing):
        mi, gj = tree.query_nearest(points[missing], return_distance=False, all_matches=False)
        hit[missing[mi]] = gj
    votes: dict[int, Counter] = defaultdict(Counter)
    for p, g in zip(pid_of, hit):
        votes[int(p)][keys[g]] += 1
    result = {}
    for pid, c in votes.items():
        key, n = c.most_common(1)[0]
        result[pid] = (key, n / sum(c.values()))
    return result


# --- population weights ----------------------------------------------------------

def city_population_by_province(table: dict[int, dict], georef: Georef, land: set[int]) -> dict[int, int]:
    """Sum of GeoNames city populations per province (nearest province centroid)."""
    seen = set()
    rows = []
    with zipfile.ZipFile(GEO / "cities15000.zip") as z:
        with z.open("cities15000.txt") as f:
            for line in io.TextIOWrapper(f, encoding="utf-8"):
                c = line.split("\t")
                if c[0] in seen:
                    continue
                seen.add(c[0])
                rows.append((float(c[4]), float(c[5]), int(c[14] or 0)))
    ids = sorted(land)
    cxy = np.array([(table[p]["x"], table[p]["y"]) for p in ids])
    cll = georef(cxy)
    # nearest province by great-circle distance on a unit sphere
    def xyz(lat, lon):
        la, lo = np.radians(lat), np.radians(lon)
        return np.column_stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])
    tree = cKDTree(xyz(cll[:, 0], cll[:, 1]))
    r = np.array(rows)
    _, idx = tree.query(xyz(r[:, 0], r[:, 1]))
    out: dict[int, int] = defaultdict(int)
    for i, pop in zip(idx, r[:, 2]):
        out[ids[i]] += int(pop)
    return out


def wikidata_population(qids: list[str]) -> dict[str, float]:
    """Population closest to 2000 for each Wikidata item (cached in tools/cache/geo)."""
    import urllib.parse
    import urllib.request

    cache_path = GEO / "wikidata_population.json"
    cache: dict[str, list] = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    import time

    todo = sorted({q for q in qids if q and q not in cache})
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&props=claims&format=json&ids="
               + urllib.parse.quote("|".join(batch)))
        req = urllib.request.Request(url, headers={"User-Agent": "TND-HOI4-mod/1.0 (admin layout tool)"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    entities = json.loads(resp.read()).get("entities", {})
                break
            except Exception:  # noqa: BLE001 - retried with backoff
                if attempt == 4:
                    raise
                time.sleep(5 * 2 ** attempt)
        for q in batch:
            best = None  # (distance from 2000, population, year)
            for c in entities.get(q, {}).get("claims", {}).get("P1082", []):
                try:
                    pop = float(c["mainsnak"]["datavalue"]["value"]["amount"])
                except (KeyError, TypeError, ValueError):
                    continue
                year = 2100.0  # undated values are a last resort
                for qual in c.get("qualifiers", {}).get("P585", []):
                    try:
                        year = float(qual["datavalue"]["value"]["time"][1:5])
                    except (KeyError, TypeError, ValueError):
                        pass
                cand = (abs(year - 2000), pop, year)
                if best is None or cand < best:
                    best = cand
            cache[q] = [best[1], best[2]] if best else [None, None]  # [population, year]
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        time.sleep(0.5)
        if (i // 50) % 10 == 0:
            print(f"  wikidata: {min(i + 50, len(todo))}/{len(todo)}")
    return {q: v[0] for q, v in cache.items() if q in set(qids) and v[0]}


def division_populations() -> dict[tuple[str, str], float]:
    """Population per division key; a division is unknown if any of its parts is."""
    _, _, keys, qids = load_divisions()
    pops = wikidata_population(qids)
    total: dict[tuple[str, str], float] = defaultdict(float)
    unknown: set[tuple[str, str]] = set()
    for k, q in zip(keys, qids):
        if q in pops:
            total[k] += pops[q]
        else:
            unknown.add(k)
    return {k: v for k, v in total.items() if k not in unknown}


def category_for(pop: int, urban: int) -> str:
    if urban >= 10_000_000:
        return "megalopolis"
    if urban >= 4_000_000:
        return "metropolis"
    if urban >= 1_500_000:
        return "large_city"
    if urban >= 500_000:
        return "city"
    if pop >= 2_000_000:
        return "large_town"
    if pop >= 500_000:
        return "town"
    if pop >= 100_000:
        return "rural"
    if pop >= 10_000:
        return "pastoral"
    return "wasteland"


# --- layout ------------------------------------------------------------------------

def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hoi4", type=Path, default=os.environ.get("HOI4_PATH"), required=os.environ.get("HOI4_PATH") is None)
    ap.add_argument("--report", action="store_true", help="dry run")
    ap.add_argument("--check-geo", action="store_true")
    ap.add_argument("--only", nargs="*", help="limit to these owner tags")
    ap.add_argument("--min-pop", type=int, default=250_000,
                    help="a new state of a single province needs at least this many people (hybrid layout)")
    args = ap.parse_args()

    table = province_geo.load(args.hoi4)
    georef = build_georef(args.hoi4, table, verbose=True)
    if args.check_geo:
        return 0

    all_states = {s.id: s for s in st.load_all()}
    land = {p for s in all_states.values() for p in s.provinces if table.get(p, {}).get("type") == "land"}
    print("assigning provinces to divisions...")
    div = province_divisions(args.hoi4, table, georef, land)
    city_pop = city_population_by_province(table, georef, land)

    # curated splits are authoritative; admin_* files are ours and get regenerated
    curated = set()
    for path in (DATA / "splits").glob("*.csv"):
        if not path.name.startswith("admin_"):
            curated |= {int(r["province"]) for r in read_csv(path)[1]}

    # state rows by id (existing) and the region file each lives in
    new_ids = {r["key"]: int(r["id"]) for r in read_csv(DATA / "new_state_ids.csv")[1]}
    region_rows: dict[str, tuple[list[str], list[dict]]] = {}
    row_of: dict[int, tuple[str, dict]] = {}
    for path in sorted((DATA / "states").glob("*.csv")):
        if path.name.startswith("admin_"):
            continue
        fields, rows = read_csv(path)
        region_rows[path.stem] = (fields, rows)
        for r in rows:
            sid = new_ids.get(r["id"]) if r["id"].startswith("new:") else int(r["id"])
            if sid is not None:
                row_of[sid] = (path.stem, r)

    # previous admin output: fold old splits back so the layout is computed from scratch
    old_admin_rows: dict[str, list[dict]] = {}
    for path in (DATA / "states").glob("admin_*.csv"):
        old_admin_rows[path.stem[6:]] = read_csv(path)[1]
    family_extra: dict[int, int] = defaultdict(int)  # parent id -> manpower held by its old admin children
    for rows in old_admin_rows.values():
        for r in rows:
            m = re.search(r"admin split from state (\d+)", r.get("notes", ""))
            if m:
                family_extra[int(m.group(1))] += int(float(r["manpower"]))
    admin_moves: dict[int, int] = {}  # province -> state it was moved from by an old admin run
    for path in (DATA / "splits").glob("admin_*.csv"):
        for r in read_csv(path)[1]:
            m = re.search(r"from state (\d+)", r.get("notes", ""))
            if m:
                admin_moves[int(r["province"])] = int(m.group(1))
    admin_new_ids = {sid for k, sid in new_ids.items() if k.startswith("new:adm_")}

    # original layout: undo previous admin moves
    state_provs: dict[int, list[int]] = {sid: [] for sid in all_states if sid not in admin_new_ids}
    for s in all_states.values():
        for p in s.provinces:
            src = admin_moves.get(p, s.id)
            if src in state_provs:
                state_provs[src].append(p)
            else:
                state_provs.setdefault(s.id, []).append(p)
    owner = {sid: all_states[sid].owner for sid in state_provs}

    # home countries of each owner (NE a3 codes covering a real share of its land)
    counts: dict[str, Counter] = defaultdict(Counter)
    for sid, provs in state_provs.items():
        for p in provs:
            if p in div:
                counts[owner[sid]][div[p][0][0]] += 1
    home = {t: {a3 for a3, n in c.items() if n >= 3 or n >= 0.1 * sum(c.values())} for t, c in counts.items()}

    def unit(p: int, sid: int) -> tuple[str, str] | None:
        d = div.get(p)
        if d is None or d[0][0] not in home.get(owner[sid], set()):
            return None
        return d[0]

    pixels = {p: table[p]["pixels"] for p in land}
    total_pop = {sid: int(float(row_of[sid][1]["manpower"])) + family_extra.get(sid, 0)
                 for sid in state_provs if sid in row_of}

    def weights(sid: int) -> dict[int, float]:
        provs = [p for p in state_provs[sid] if p in land]
        pop = total_pop.get(sid, 0)
        urban = sum(city_pop.get(p, 0) for p in provs)
        rural = max(pop - urban, 0.3 * pop)
        area = sum(pixels[p] for p in provs) or 1
        return {p: city_pop.get(p, 0) * (pop / max(urban, pop) if urban > pop else 1) + rural * pixels[p] / area
                for p in provs}

    # population estimate per province: its division's real (Wikidata, ~2000) population,
    # shared out by the city/area weights; provinces of unknown divisions use the weights alone
    print("division populations...")
    div_pop = division_populations()
    pw: dict[int, float] = {}
    for sid in state_provs:
        if sid in row_of:
            pw.update(weights(sid))
    div_w: dict[tuple[str, str], float] = defaultdict(float)
    for p, v in pw.items():
        if p in div:
            div_w[div[p][0]] += v

    def est(p: int) -> float:
        k = div[p][0] if p in div else None
        if k in div_pop and div_w.get(k):
            return div_pop[k] * pw.get(p, 0) / div_w[k]
        return pw.get(p, 0)

    # dominant division of every state (by population weight)
    dominant: dict[int, tuple[str, str] | None] = {}
    parts: dict[int, dict[tuple[str, str], list[int]]] = {}
    for sid in state_provs:
        if args.only and owner[sid] not in args.only:
            continue
        w = weights(sid)
        by_unit: dict[tuple[str, str] | None, list[int]] = defaultdict(list)
        for p in state_provs[sid]:
            by_unit[unit(p, sid) if p not in curated else None].append(p)
        scored = {u: sum(w.get(p, 0) for p in ps) for u, ps in by_unit.items() if u is not None}
        dom = max(scored, key=scored.get) if scored else None
        dominant[sid] = dom
        parts[sid] = {u: ps for u, ps in by_unit.items() if u is not None and u != dom}

    # states by (owner, division) they are dominated by, for merging parts into neighbours
    by_owner_unit: dict[tuple[str, tuple[str, str]], list[int]] = defaultdict(list)
    for sid, dom in dominant.items():
        if dom is not None:
            by_owner_unit[(owner[sid], dom)].append(sid)
    neighbours = {p: set(table[p]["neighbours"]) for p in land}

    moves: dict[int, tuple[str, int]] = {}  # province -> (target key, source state)
    new_states: dict[tuple[str, tuple[str, str]], dict] = {}
    for sid, us in sorted(parts.items()):
        for u, provs in us.items():
            provs = [p for p in provs if p in land]
            if not provs:
                continue
            target = None
            touching = {q for p in provs for q in neighbours[p]}
            for cand in by_owner_unit.get((owner[sid], u), []):
                if touching & set(state_provs[cand]):
                    target = str(cand)
                    break
            if target is None:
                key = f"new:adm_{owner[sid].lower()}_{slug(u[1])}"
                ns = new_states.setdefault((owner[sid], u), {"key": key, "name": u[1], "parents": Counter(), "provs": []})
                ns["parents"][sid] += len(provs)
                ns["provs"] += provs
                target = key
            for p in provs:
                moves[p] = (target, sid)

    # hybrid floor: a one-province new state with few people stays with its parent
    for (tag, u), ns in list(new_states.items()):
        if len(ns["provs"]) == 1 and est(ns["provs"][0]) < args.min_pop:
            for p in ns["provs"]:
                moves.pop(p, None)
            del new_states[(tag, u)]

    # population shares
    moved_weight: dict[tuple[int, str], float] = defaultdict(float)
    kept_weight: dict[int, float] = {}
    urban_of: dict[str, int] = defaultdict(int)
    for sid in parts:
        w = {p: est(p) for p in weights(sid)}
        kept_weight[sid] = sum(v for p, v in w.items() if p not in moves)
        for p, v in w.items():
            if p in moves:
                moved_weight[(sid, moves[p][0])] += v
                urban_of[moves[p][0]] += city_pop.get(p, 0)

    report = defaultdict(lambda: [0, 0])
    new_rows_by_region: dict[str, list[dict]] = defaultdict(list)
    merged_into: dict[str, float] = defaultdict(float)
    for sid in parts:
        tot = kept_weight[sid] + sum(v for (s, _), v in moved_weight.items() if s == sid)
        if tot <= 0 or sid not in row_of:
            continue
        pop = total_pop[sid]
        for (s, tgt), v in moved_weight.items():
            if s == sid:
                merged_into[tgt] += pop * v / tot
        region, row = row_of[sid]
        kept = max(1, round(pop * kept_weight[sid] / tot))
        old_mp = int(float(row["manpower"]))
        row["manpower"] = str(kept)
        if kept < 0.75 * pop:
            urban = sum(city_pop.get(p, 0) for p in state_provs[sid] if p not in moves)
            row["category"] = category_for(kept, urban)
        if kept != old_mp:
            report[owner[sid]][0] += 1

    # existing states that absorbed parts gain their people
    for tgt, extra in merged_into.items():
        if tgt.startswith("new:"):
            continue
        region, row = row_of[int(tgt)]
        row["manpower"] = str(int(float(row["manpower"])) + round(extra))

    for (tag, u), ns in sorted(new_states.items(), key=lambda kv: kv[1]["key"]):
        parent = ns["parents"].most_common(1)[0][0]
        region, prow = row_of[parent]
        pop = max(1, round(merged_into[ns["key"]]))
        new_rows_by_region[region].append({
            "id": ns["key"], "owner": prow["owner"], "controller": "", "cores": prow["cores"],
            "claims": prow["claims"], "manpower": str(pop),
            "category": category_for(pop, urban_of[ns["key"]]), "name": ns["name"],
            "notes": f"admin split from state {parent} ({u[0]} first-level division {u[1]})",
        })
        report[tag][1] += 1

    total_new = sum(len(v) for v in new_rows_by_region.values())
    sizes = Counter(min(len(ns["provs"]), 4) for ns in new_states.values())
    small = sum(1 for ns in new_states.values() if len(ns["provs"]) == 1 and merged_into[ns["key"]] < 250_000)
    print(f"new state sizes (provinces, 4 = 4+): {dict(sorted(sizes.items()))}; "
          f"single-province under 250k people: {small}")
    print(f"{len(moves)} province moves, {total_new} new states "
          f"({len(all_states) - len(admin_new_ids)} existing)")
    for tag, (changed, created) in sorted(report.items(), key=lambda kv: -kv[1][1])[:60]:
        print(f"  {tag}: {created} new states, {changed} states resized")
    if args.only:  # detail for a focused run
        for sid in sorted(parts):
            print(f"state {sid} ({row_of.get(sid, ('', {}))[1].get('name')}): keeps {dominant[sid]}")
            for u, provs in parts[sid].items():
                print(f"    -> {u[1]}: {len(provs)} provinces -> {moves.get(provs[0], ('?',))[0]}")
        for (tag, u), ns in new_states.items():
            print(f"new {ns['key']}: {len(ns['provs'])} provinces, pop {round(merged_into[ns['key']]):,}")
    if args.report:
        return 0

    for region, (fields, rows) in region_rows.items():
        write_csv(DATA / "states" / f"{region}.csv", fields, rows)
    fields = ["id", "owner", "controller", "cores", "claims", "manpower", "category", "name", "notes"]
    by_region_moves: dict[str, list[dict]] = defaultdict(list)
    for p, (tgt, src) in sorted(moves.items()):
        by_region_moves[row_of[src][0]].append({"province": p, "to_state": tgt, "notes": f"admin: from state {src}"})
    # stale regions keep an empty (header-only) file rather than being deleted
    for path in (DATA / "states").glob("admin_*.csv"):
        write_csv(path, fields, [])
    for path in (DATA / "splits").glob("admin_*.csv"):
        write_csv(path, ["province", "to_state", "notes"], [])
    for region, rows in new_rows_by_region.items():
        write_csv(DATA / "states" / f"admin_{region}.csv", fields, rows)
    for region, rows in by_region_moves.items():
        write_csv(DATA / "splits" / f"admin_{region}.csv", ["province", "to_state", "notes"], rows)
    print("data written; next: python tools/apply_state_data.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
