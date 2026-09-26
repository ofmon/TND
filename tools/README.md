# TND tooling

These are Python 3.10+ scripts. They need `numpy` and `pillow` for the map steps. Point them at the game install with `--hoi4` or the `HOI4_PATH` environment variable.

```powershell
$env:HOI4_PATH = "E:\SteamLibrary\steamapps\common\Hearts of Iron IV"
```

## Everyday workflow: changing state owners, populations or categories

1. Edit the CSVs in `data/states/` and `data/splits/`. `data/README.md` describes the format and conventions.
2. `python tools/apply_state_data.py --check` validates the data without writing anything.
3. `python tools/apply_state_data.py` writes the data into `history/states`. Running it again with the same data changes nothing.
4. If provinces moved or new states were created, run `python tools/sync_strategic_regions.py` and `python tools/sync_building_positions.py`. For renamed places, edit `data/modern_names/*.csv` and run `python tools/build_modern_names.py`.
5. `python tools/validate_states.py` must report 0 errors before you commit.
6. Optionally, `python tools/report_state_data.py` compares national population totals with `data/reference_population_2000.csv`.
7. Optionally, `python tools/install_test_mod.py` copies a snapshot into the HOI4 launcher's mod folder as "TND (test build)".

Don't hand-edit owner, cores, manpower or state_category inside `history/states/*.txt`. The next apply overwrites them from the CSVs. Everything else in the state files (buildings, VPs, resources) is safe to edit by hand.

## Scripts

| Script | Purpose |
|---|---|
| `clausewitz.py` | Parser for Paradox script that records where each entry sits in the source text |
| `states.py` | State data model and loader |
| `state_file.py` | Edits state files while preserving their comments and formatting |
| `apply_state_data.py` | Applies `data/` to `history/states`, including province splits and new state IDs |
| `validate_states.py` | Checks for merge markers, parse errors, ID gaps and duplicates, province coverage, tags, localisation and capitals |
| `report_state_data.py` | Cross-region population check |
| `province_geo.py` | Province centroids, neighbours and approximate lat/lon, cached in `cache/` |
| `export_state_table.py` | Writes per-region state and province tables to `cache/regions/` for research |
| `sync_strategic_regions.py` | Keeps each state inside one strategic region; a state spanning two crashes the game |
| `build_modern_names.py` | Generates 2000-era state and city names from `data/modern_names/` |
| `buildings.py` | Checks building validity against `common/buildings` |
| `build_formables.py` | Generates formable-nation decisions, localisation and flags from `data/formables.json` |
| `sync_building_positions.py` | Regenerates `map/buildings.txt` for split states |
| `install_test_mod.py` | Installs a test copy into the HOI4 mod folder |
| `rebase_states.py`, `strip_1936_effects.py` | One-time migrations to game 1.19 and the 2000 start; kept for reference |
