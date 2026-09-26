# State data (start date 2000.1.1)

Curated source of truth for every state's ownership, population and category.
`tools/apply_state_data.py` writes it into `history/states/`; never hand-edit
those fields in the state files, or the next apply will overwrite them.

Workflow:

```
python tools/apply_state_data.py --check   # validate data only
python tools/apply_state_data.py           # apply (idempotent)
python tools/validate_states.py --hoi4 "<HOI4 install>"
```

## Files

| File | Contents |
|---|---|
| `states/<region>.csv` | `id,owner,controller,cores,claims,manpower,category,name,notes` (one row per state) |
| `splits/<region>.csv` | `province,to_state,notes` (moves a province, with its province-keyed buildings and VPs, to another state) |
| `new_state_ids.csv` | generated; permanent `new:<slug>` → state ID allocations. **Never renumber.** |
| `geo_anchors.csv` | cities used to estimate province lat/lon (`tools/province_geo.py`) |

* `id` is an existing state ID, or `new:<slug>` (lowercase, `a-z0-9_`) for a state created by a split. A new state needs a `name` and at least one province moved into it.
* `cores` and `claims` are space-separated tags. Only use tags defined in `common/country_tags`.
* Leave `controller` blank unless a state is occupied in a war that is running on the start date. HOI4 resets controllers that don't match a war.

## Conventions

**Date.** Everything reflects **1 January 2000**. The Federal Republic of Yugoslavia still includes Montenegro and Kosovo, Sudan is undivided, Crimea is Ukrainian, Hong Kong and Macau are Chinese, and East Timor is under UN transition.

**Tags that represent modern states.**

| Tag | Country |
|---|---|
| SOV | Russian Federation |
| PRC | People's Republic of China |
| TAW | Taiwan (Republic of China) |
| RAJ | India |
| SER | FR Yugoslavia (Serbia and Montenegro) |
| CZE | Czech Republic |
| SLO | Slovakia |
| COG | DR Congo |
| RCG | Republic of the Congo |
| DAH | Benin |
| VOL | Burkina Faso |
| PER | Iran |
| SIA | Thailand |
| BRM | Myanmar |
| VIN | Vietnam |
| URG | Uruguay |
| HOL | Netherlands |
| ENG | United Kingdom |

**Cores.** The owner, plus any recognised constituent or secessionist nation that has a defined tag. Examples: SER owns Kosovo with cores SER KOS, and Montenegro with cores SER MNT. MOR owns Western Sahara with a WES core. GEO owns Abkhazia with an ABK core. Don't add cores for tags that aren't defined; list the gap in `notes` instead.

**Claims.** Only live territorial disputes as of 2000, for example Kashmir, the Falklands (ARG), Taiwan (PRC), the Kurils, Gibraltar (SPR), Essequibo (VEN) and the Golan Heights (SYR).

**Manpower.** The **total resident population** of the area on 1 Jan 2000, following the vanilla convention. Split national totals across states by the real administrative regions each state covers. Every state's figure must be positive; uninhabited areas get a token value such as 100. Country totals must add up to the real 2000 national population, within about 2%.

**Category.** Use the definitions below. Judge urbanisation and development, not just headcount.

| Category | Use for |
|---|---|
| `megalopolis` | Metro area of 10M+ with global economic weight (Tokyo, New York, London, Paris, Moscow, Shanghai…) |
| `metropolis` | Metro area of 4–10M, or a major national capital region |
| `large_city` | Metro area of 1.5–4M, or a densely urbanised industrial region |
| `city` | Metro area of 0.5–1.5M, or a well-developed mixed region |
| `large_town` | Developed region without a big metro, or a dense but poorer agricultural region |
| `town` | Moderately populated rural region |
| `rural` | Sparse or poor region |
| `pastoral` | Very sparse (steppe, desert fringe, tundra with towns) |
| `wasteland` | Essentially uninhabited (ice cap, deep desert) |
| `enclave`, `tiny_island`, `small_island` | Keep these where vanilla uses them for tiny territories |

**Splits.** Only split where a modern **international** border (as of 2000) cuts a vanilla state. Decide each province by its lat/lon (from `tools/cache/regions/*.json`), its neighbours and its name. Sub-national boundaries are not a reason to split.

## Administrative layout (state shapes)

States follow **2000 first-level administrative divisions**: German Länder, US states, Chinese provinces, Indian states and union territories, Russian federal subjects, French régions, and so on. The rules:

1. **No state may cross a first-level division boundary.** A division may contain several states when it is large or when vanilla already splits it (e.g. a big Russian oblast or Canadian province).
2. **Every existing state keeps at least one province and its ID.** Deleting a state would leave a gap in the state IDs, and HOI4 crashes on gaps. It also keeps vanilla's state-ID references valid. When re-dividing, the existing state keeps the division holding most of its population or its main victory point. New divisions become `new:<slug>` states.
3. **City-states get their own state if they cover at least one province** (Berlin, Hamburg, Bremen, Vienna, Moscow city, Washington DC, Delhi...). Smaller ones merge into the surrounding division.
4. **Nested divisions** (Russian autonomous okrugs inside a krai or oblast, Indian enclaves) merge into their parent unless they're large and span several provinces.
5. Province edges follow the map's provinces, so assign each province to the division holding most of its area.
6. When a state loses or gains provinces, update its `manpower` and `category` so it describes only its new area.

Province moves go in `splits/<region>.csv` as usual. Strategic regions and building positions are then resynced by `sync_strategic_regions.py` and `sync_building_positions.py`.

## Historical layer (`historical/*.csv`)

These are cores and claims layered on top of the 2000 baseline above. The apply step merges them in, so the baseline CSVs stay a clean picture of 1 Jan 2000.

* Columns: `state,tag,type,rationale`, one row per state, tag and type. `type` is `core` or `claim`, and `state` is an ID or a `new:<slug>`.
* A `rationale` is required. It says which historical fact or movement justifies the row.
* A row may not duplicate the baseline. A country can't claim a state it owns, or claim a state where it also has a core.

**Claims are irredentist.** Add one where a country held the land within roughly the last 150 years, or where a significant nationalist movement in that country claims it. Examples: Hungary on southern Slovakia and Transylvania, Germany on Silesia and East Prussia, Russia on Crimea, Greece on Northern Epirus, Armenia on Nakhchivan, Morocco on Mauritania, Bolivia on the Litoral.

**Cores come in two kinds.**

1. **Releasable or formable homelands.** Cores for a defined sub-national or historical tag on its own homeland, so it can be released or restored. Examples: YUG on all former Yugoslav states, PRE on Brandenburg and Pomerania, BAY on Bavaria, CAT on Catalonia, KUR on Kurdish areas.
2. **Large cross-border co-ethnic populations.** A country gets a core where its nationality is a majority, or a very large and concentrated minority, of a neighbouring state's population. Examples: Hungary on Székely Land, Albania on Kosovo, Russia on Crimea and north-eastern Estonia, Armenia on Karabakh, Somalia on the Ogaden.

## Formables (`formables.json`)

This file specifies new formable nations. `python tools/build_formables.py` generates their decisions, localisation and placeholder flags. The format is documented at the top of that script.
