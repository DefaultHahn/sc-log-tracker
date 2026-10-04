"""Turning internal Star Citizen names into readable ones."""
import pytest

import sc_log_tracker as t


@pytest.mark.parametrize("code, expected", [
    ("RR_P3_LEO", "Orbituary"),
    ("RR_P3_L1", "Starlight Service Station"),
    ("RR_P2_L4", "Checkmate"),
    ("RR_CRU_LEO", "Seraphim Station"),
    ("RR_HUR_LEO", "Everus Harbor"),
    ("RR_MIC_LEO", "Port Tressler"),
    ("RR_HUR_L1", "HUR-L1"),
    ("RR_P6_LEO", "Rest stop Terminus (orbit)"),
    ("RR_JP_StantonPyro", "Pyro Gateway"),
    ("RR_JP_PyroStanton", "Stanton Gateway"),
    ("rs_ext_pyro3_leo", "Orbituary"),
    ("rs_ext_cru-leo1", "Seraphim Station"),
    ("rs_ext_pyro-stan_jp1", "Stanton Gateway"),
    ("GrimHEX", "Grim HEX"),
    ("Nyx_Levski", "Levski"),
    ("Nyx_TSG_QVExtractionStation_024", "TSG QV Extraction Station"),
    ("OOC_Stanton_2c_Yela", "Yela"),
    ("OOC_Stanton_4_Microtech", "microTech"),
    ("Outpost_OLP_Stanton2b_Attritus", "Attritus (Daymar)"),
    ("Outpost_PAF_Stanton2b_Attritus_3", "Attritus (Daymar)"),
    ("Stanton4b_RayariHydro_McGarth", "Rayari Hydro McGarth (Clio)"),
    ("Stanton2b_ASD_Delve_Facility_009", "ASD Delve Facility (Daymar)"),
    ("Stanton3_Area18", "Area18 (ArcCorp)"),
    ("PrisonMine_Stanton1b", "Prison Mine (Aberdeen)"),
    ("Pyro1_ASD_Monorail_LazarusTransportHub_Tithonus_2C", "Lazarus Transport Hub Tithonus 2C (Pyro I)"),
    ("Pyro4_Outpost_col_m_scrp_indy_001", "Outpost (Pyro IV)"),
    ("Pyro5a_Outpost_col_m_trdpst_otlw_001", "Trading post (Ignis)"),
    ("pyro5e", "Fuego"),
])
def test_pretty_loc(code, expected):
    assert t.pretty_loc(code) == expected


@pytest.mark.parametrize("code, expected", [
    ("PartyMemberMarker_783805866737", ("Party member", False)),
    ("NavPoint_Dynamic_833814974887", ("Marked nav point", False)),
    ("orbtl_002_plat_001_util_a_orbital_001_occu_a_final", ("Mission target", False)),
    ("MISSION_QT_Kaboos_834249898820", ("Mission target (Kaboos)", False)),
    ("AsteroidCluster_Pyro_RegionB_BGR-560", ("Asteroid field BGR-560 (Pyro)", False)),
    ("LOC_RR_S4_L5", ("MIC-L5", True)),
    ("GrimHEX_OC", ("Grim HEX", True)),
    ("levski_all-001", ("Levski", True)),
    ("OOC_Stanton_2b_Daymar", ("Daymar", True)),
])
def test_pretty_dest(code, expected):
    assert t.pretty_dest(code) == expected


@pytest.mark.parametrize("cls, expected", [
    ("AEGS_Gladius_1234567", "Aegis Gladius"),
    ("ANVL_C8R_Pisces_855968667336", "Anvil C8R Pisces"),
    ("@vehicle_NameANVL_C8R_Pisces_Rescue", "Anvil C8R Pisces Rescue"),
    ("DRAK_Corsair_Exec_StealthIndustrial_855781285820", "Drake Corsair Exec Stealth Industrial"),
])
def test_pretty_class(cls, expected):
    assert t.pretty_class(cls) == expected


@pytest.mark.parametrize("cls, expected", [
    ("lbco_sniper_energy_01_mag", "Lightning Bolt Sniper Energy (magazine)"),
    ("behr_rifle_ballistic_02_mag_civilian", "Behring Rifle Ballistic (magazine)"),
    ("crlf_medgun_01", "CureLife Medgun"),
    ("Mining_Gadget_SHIN_Sabir", "Mining Gadget Shubin Sabir"),
])
def test_pretty_item(cls, expected):
    assert t.pretty_item(cls) == expected


def test_pretty_shop():
    assert t.pretty_shop("SCShop_Pyro_RestStop_BlackMarket_FPSItems") == "Pyro Rest Stop Black Market FPS Items"
    assert t.pretty_shop("SCShop_RestStop_Pharmacy-004") == "Rest Stop Pharmacy"


def test_clean_notif_strips_language_pack_markup():
    raw = "Contract Shared: Tactical Strike Group Needed <EM4>[300 Rep] [BP]</EM4>: "
    assert t.clean_notif(raw) == "Contract Shared: Tactical Strike Group Needed"
