version="1"
tags={
	"Alternative History"
	"Events"
	"Gameplay"
	"Historical"
	"Map"
	"National Focuses"
	"Technologies"
	"Military"
	"Ideologies"
}
name="TND"
# Only replace a folder the mod ships a complete replacement for: replace_path
# deletes every vanilla file in it, and anything still referencing that content
# (history set_technology, vanilla ideas/triggers, ...) breaks.
replace_path="common/bookmarks"
replace_path="common/decisions"
replace_path="common/decisions/categories"
replace_path="common/national_focus"
replace_path="history/countries"
# The mod ships every state (see tools/validate_states.py); vanilla files with
# other names would otherwise load too and define the same state ID twice.
replace_path="history/states"
replace_path="events"
supported_version="1.19.*"
