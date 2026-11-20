REGION_ORDER = ("Europe", "Asia", "Americas", "Africa", "Oceania", "Other")


def _region_map(region, codes):
    return {code: region for code in codes.split()}


STORY_MAP_REGIONS = {
    **_region_map(
        "Europe",
        "AL AD AT BY BE BA BG HR CY CZ DK EE FI FR DE GR HU IS IE IT LV LI LT "
        "LU MT MD MC ME MK NL NO PL PT RO RU SM RS SK SI ES SE CH TR UA GB VA",
    ),
    **_region_map(
        "Asia",
        "AF AM AZ BH BD BT BN KH CN GE IN ID IR IQ IL JP JO KZ KP KR KW KG LA "
        "LB MY MV MN MM NP OM PK PS PH QA SA SG LK SY TW TJ TH TL TM AE UZ VN YE",
    ),
    **_region_map(
        "Americas",
        "AG AR BS BB BZ BO BR CA CL CO CR CU DM DO EC SV GD GT GY HT HN JM KN "
        "LC MX NI PA PY PE SR TT US UY VC VE",
    ),
    **_region_map(
        "Africa",
        "DZ AO BJ BW BF BI CV CM CF TD KM CG CD CI DJ EG GQ ER SZ ET GA GM GH GN "
        "GW KE LS LR LY MG MW ML MR MU MA MZ NA NE NG RW ST SN SC SL SO ZA SS SD "
        "TZ TG TN UG ZM ZW",
    ),
    **_region_map(
        "Oceania",
        "AU FJ KI MH FM NR NZ PW PG WS SB TO TV VU",
    ),
}


def story_map_region(country_code):
    return STORY_MAP_REGIONS.get(country_code, "Other")
