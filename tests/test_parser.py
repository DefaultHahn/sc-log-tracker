"""Parsing real-format Game.log lines into events."""
from conftest import DEMO_LOG

import sc_log_tracker as t


def parse(lines):
    p = t.Parser()
    events = []
    for line in lines:
        events.extend(p.feed(line))
    return p, events


def titles(events):
    return [e["ti"] for e in events]


def demo():
    with open(DEMO_LOG, "rb") as f:
        return parse(raw.decode("utf-8").rstrip("\r\n") for raw in f)


def test_demo_session_state():
    p, events = demo()
    s = p.state
    assert s["handle"] == "DemoPilot"
    assert s["channel"] == "LIVE"
    assert s["version"] == "4.10.193.11644"
    assert s["shard"] == "pub_euw1b_12660092_100"
    assert s["qt_jumps"] == 2
    assert s["contracts_acc"] == 1 and s["contracts_done"] == 1
    assert s["deaths"] == 1
    assert s["purchases"] == 1 and s["spend"] == 1325.0
    assert s["received"] == 50000
    assert s["blueprints"] == 1
    assert s["crimes"] == 2


def test_demo_has_every_category():
    _, events = demo()
    cats = {e["c"] for e in events}
    assert {"session", "travel", "ship", "mission", "economy", "combat", "law", "social"} <= cats


def test_no_unrecognized_hud_notices_in_demo():
    _, events = demo()
    assert [e["d"] for e in events if e["ti"] == "HUD notice"] == []


def test_multiline_notification():
    lines = [
        '<2026-10-03T21:50:13.892Z> [Notice] <SHUDEvent_OnNotification> Added notification "Party',
        '<2026-10-03T21:50:13.892Z> StormPilot connected.: " [8] to queue. New queue size: 1',
        '<2026-10-03T21:50:13.892Z>    "Party',
    ]
    _, events = parse(lines)
    assert titles(events) == ["Party member online"]
    assert events[0]["d"] == "StormPilot"


def test_notification_ids_are_deduplicated():
    line = '<2026-10-03T20:00:00.000Z> [Notice] <SHUDEvent_OnNotification> Added notification "Entered UEE Jurisdiction: " [18] to queue.'
    _, events = parse([line, line])
    assert titles(events) == ["Jurisdiction"]


def test_quantum_target_and_arrival():
    lines = [
        "<2026-10-03T17:35:19.316Z> [Notice] <Player Selected Quantum Target - Local> [ItemNavigation][CL][1] | NOT AUTH | ANVL_C8R_Pisces_855968667336[855968667336]|CSCItemNavigation::OnPlayerSelectedQuantumTarget|Player has selected point OOC_Stanton_2b_Daymar as their destination, routing locally",
        "<2026-10-03T17:36:27.847Z> [Notice] <Quantum Drive Arrived - Arrived at Final Destination> [ItemNavigation][CL][1] | NOT AUTH | ANVL_C8R_Pisces_855968667336[855968667336]|CSCItemNavigation::OnQuantumDriveArrived|Quantum Drive has arrived at final destination",
    ]
    p, events = parse(lines)
    assert titles(events) == ["Quantum target set", "Quantum jump arrived"]
    assert events[1]["d"] == "Daymar"
    assert p.state["location"] == "Daymar"
    assert p.state["pilot_ship"] == "Anvil C8R Pisces"


def test_quantum_arrival_as_crew():
    line = "<2026-10-01T16:10:39.874Z> [Notice] <Quantum Drive Arrived - Arrived at Final Destination> [ItemNavigation][CL][1] | NOT AUTH | RSI_Polaris_852976905726[852976905726]|CSCItemNavigation::OnQuantumDriveArrived|Quantum Drive has arrived at final destination"
    _, events = parse([line])
    assert events[0]["d"] == "aboard RSI Polaris, target set by the pilot"


def test_releasing_control_token_is_not_taking_controls():
    line = "<2026-10-03T21:59:37.400Z> [Notice] <Vehicle Control Flow> CVehicleMovementBase::ClearDriver: Local client node [1] releasing control token for 'DRAK_Corsair_855781285820' [855781285820] [Team_CGP4][Vehicle]"
    _, events = parse([line])
    assert titles(events) == ["Left pilot seat"]


def test_jurisdiction_ping_pong_is_suppressed():
    def j(sec, name, nid):
        return f'<2026-10-03T20:00:{sec:02d}.000Z> [Notice] <SHUDEvent_OnNotification> Added notification "Entered {name} Jurisdiction: " [{nid}] to queue.'
    _, events = parse([j(0, "Rough & Ready", 1), j(5, "Ungoverned", 2), j(9, "Rough & Ready", 3), j(12, "Ungoverned", 4)])
    assert [e["d"] for e in events] == ["Rough & Ready", "Ungoverned (lawless)"]


def test_party_reconnects_are_not_repeated():
    def party(sec, nid):
        return [f'<2026-10-03T20:00:{sec:02d}.000Z> [Notice] <SHUDEvent_OnNotification> Added notification "Party',
                f'<2026-10-03T20:00:{sec:02d}.000Z> Friend1 connected.: " [{nid}] to queue.']
    _, events = parse(party(0, 1) + party(30, 2))
    assert titles(events) == ["Party member online"]


def test_crash_report_without_timestamps():
    lines = [
        "<2026-10-03T20:00:00.000Z> [Notice] something",
        "Cloud Imperium Games public crash handler taking over...",
        "Exception STATUS_CRYENGINE_WATCH_DOG(0x2BADFF60) addr=0x00007FFEE47941CA digest=abc",
    ]
    p, events = parse(lines)
    assert titles(events) == ["Crash!", "Crash cause"]
    assert events[1]["d"] == "Game hung (watchdog timeout)"
    assert p.state["crashes"] == 1


def test_disconnect_pair_counts_once():
    a = '<2026-10-03T20:00:00.000Z> [Notice] <Channel Disconnected> cause=30016 reason="DisconnectCmd: disconnect light ExitToMenu" isRemote=0 gamerules="SC_Default"'
    b = '<2026-10-03T20:00:01.000Z> [Notice] <Channel Disconnected> cause=30016 reason="Remote Disconnect - Player requested disconnect" isRemote=1 gamerules="SC_Default"'
    p, events = parse([a, b])
    assert titles(events) == ["Exited to menu"]
    assert p.state["disconnects"] == 1


def test_frontend_noise_is_ignored():
    line = '<2026-10-03T21:47:18.314Z> [Notice] <Channel Disconnected> cause=30010 reason="Nub destroyed" isRemote=0 gamerules="SC_Frontend"'
    _, events = parse([line])
    assert events == []


def test_legacy_kill_line():
    lines = [
        "<2025-03-01T10:00:00.000Z> [Notice] <Legacy login response> [CIG-net] User Login Success - Handle[Me] - Time[1]",
        "<2025-03-01T10:05:00.000Z> [Notice] <Actor Death> CActor::Kill: 'Pirate_1' [1] in zone 'OOC_Stanton_2c_Yela' killed by 'Me' [2] using 'KLWE_LaserRepeater_S3_1234' [Class KLWE_LaserRepeater_S3] with damage type 'VehicleDestruction' from direction x: 0",
    ]
    p, events = parse(lines)
    assert events[-1]["ti"] == "Kill"
    assert p.state["kills"] == 1


def test_legacy_kills_of_others_are_hidden():
    lines = [
        "<2025-03-01T10:00:00.000Z> [Notice] <Legacy login response> [CIG-net] User Login Success - Handle[Me] - Time[1]",
        "<2025-03-01T10:05:00.000Z> [Notice] <Actor Death> CActor::Kill: 'NPC_1' [1] in zone 'x' killed by 'NPC_2' [2] using 'KLWE_LaserRepeater_S3_1234' [Class KLWE_LaserRepeater_S3] with damage type 'Bullet' from direction x: 0",
    ]
    _, events = parse(lines)
    assert titles(events) == ["Logged in"]


def test_hotfix_version_comes_from_branch():
    lines = [
        "<2026-03-30T19:40:49.835Z> FileVersion: 1.0.176.11384",
        "<2026-03-30T19:41:01.397Z> Branch: sc-alpha-4.7.0-hotfix",
    ]
    p, events = parse(lines)
    assert p.state["version"] == "4.7.0-hotfix"
    assert titles(events) == ["Game version"]


def test_more_notifications():
    def n(text, nid):
        return f'<2026-10-03T20:00:{nid:02d}.000Z> [Notice] <SHUDEvent_OnNotification> Added notification "{text}: " [{nid}] to queue.'
    _, events = parse([n("Objective Failed: Escort Ship to Hangar", 1), n("Contract Withdrawn: Reduce Overpopulation", 2),
                       n("You have left the party.", 3),
                       n("Item Bricked: Your RSI Polaris and 59 attached item(s) are now bricked and will no longer function.", 4)])
    assert titles(events) == ["Objective failed", "Contract withdrawn", "Left party", "Item bricked"]


def test_every_server_join_is_an_event():
    def j(m, shard):
        return f"<2026-10-03T20:{m:02d}:00.000Z> [Notice] <Join PU> address[192.0.2.1] port[1] shard[{shard}] locationId[1]"
    p, events = parse([j(0, "pub_euw1b_1_110"), j(5, "pub_euw1b_1_110"), j(9, "pub_use1b_1_250")])
    assert titles(events) == ["Joined server"] * 3
    assert [e["x"] for e in events] == ["pub_euw1b_1_110", "pub_euw1b_1_110", "pub_use1b_1_250"]
    assert events[2]["d"] == "pub_use1b_1_250 · US East"
    assert p.state["region"] == "US East"


PLACE = ("<2026-10-02T16:34:46.000Z> [Notice] <[ActorState] Place> [ACTOR STATE][CSCActorControlAdditiveStatePlace::DoPlace] "
         "'{who}' [204] placed '{item}_851234567890' [851234567890] in lootable container '{box}_833462956612' [833462956612] "
         "[Team_ActorFeatures][Actor]")


def test_activities_from_placed_items():
    me = '<2026-10-02T16:00:00.000Z> [Notice] <Legacy login response> [CIG-net] User Login Success - Handle[Me] - Time[1]'
    lines = [me,
             PLACE.format(who="Me", item="Fuse_subItem_standard", box="GPI_Relay_1slot_001_CZ_Degradation"),
             PLACE.format(who="Me", item="Carryable_2H_FL_Rdt_Vlk_Egg", box="Stmgn_Pylon_Hatch"),
             PLACE.format(who="Me", item="FPS_Consumable_KeyCard_SOO_WeaponCache", box="Slot_Removable_Chip_Access_SOO_WeaponCache"),
             PLACE.format(who="Me", item="ORI_game_chess", box="game_chess_1_a-001"),            # nothing special
             PLACE.format(who="Someone", item="Fuse_subItem_standard", box="GPI_Door_1slot")]    # not you
    _, events = parse(lines)
    acts = [(e["ti"], e["d"], e["x"]) for e in events if e["c"] == "activity"]
    assert acts == [("Fuse inserted", "contested zone relay", "fuse"), ("Valakkar egg delivered", "", "egg"),
                    ("Keycard used", "weapon cache", "keycard")]


def test_mission_end_carries_the_contract_name():
    mid = "c40dedbc-ab90-4d3c-98cc-ce9e91ab3d93"
    lines = [
        f'<2026-10-02T22:00:00.000Z> [Notice] <SHUDEvent_OnNotification> Added notification "Contract Accepted: Kill the king: " [5] to queue. New queue size: 1, MissionId: [{mid}], ObjectiveId: []',
        f"<2026-10-02T22:30:00.000Z> [Notice] <EndMission> Ending mission for player. MissionId[{mid}] Player[Me] PlayerId[1] CompletionType[Fail] Reason[Mission Ended]",
        "<2026-10-02T22:31:00.000Z> [Notice] <EndMission> Ending mission for player. MissionId[11111111-ab90-4d3c-98cc-ce9e91ab3d93] Player[Me] PlayerId[1] CompletionType[Abandon] Reason[Mission Ended]",
    ]
    _, events = parse(lines)
    ended = [(e["d"], e["x"]) for e in events if e["ti"] == "Mission ended"]
    assert ended == [("failed: Kill the king", "failed"), ("abandoned", "abandoned")]


def test_location_and_purchase_carry_machine_readable_values():
    lines = [
        "<2026-10-02T16:00:00.000Z> [Notice] <RequestLocationInventory> Player[Me] requested inventory for Location[Pyro1_ASD_Monorail_LazarusTransportHub_Tithonus_2B] [Team]",
        "<2026-10-02T16:10:00.000Z> [Notice] <RequestLocationInventory> Player[Me] requested inventory for Location[Nyx_Levski] [Team]",
        "<2026-10-02T16:20:00.000Z> [Notice] <CEntityComponentShopUIProvider::SendShopBuyRequest> Sending SShopBuyRequest - playerId[1] shopId[2] shopName[SCShop_Levski_Electronics] kioskId[3] client_price[2550.000000] itemClassGUID[x] itemName[bltr_consumable_hackingchip] quantity[1]",
    ]
    _, events = parse(lines)
    assert [(e["d"], e["x"]) for e in events if e["ti"] == "Location"] == [
        ("Lazarus Transport Hub Tithonus 2B (Pyro I)", "Pyro"), ("Levski", "Nyx")]
    assert [e["x"] for e in events if e["ti"] == "Purchase"] == ["2550"]


def test_quantum_arrival_at_a_real_place_names_it():
    sel = "<2026-10-03T17:35:19.316Z> [Notice] <Player Selected Quantum Target - Local> [ItemNavigation][CL][1] | NOT AUTH | ANVL_C8R_Pisces_855968667336[855968667336]|CSCItemNavigation::OnPlayerSelectedQuantumTarget|Player has selected point {} as their destination, routing locally"
    arr = "<2026-10-03T17:36:27.847Z> [Notice] <Quantum Drive Arrived - Arrived at Final Destination> [ItemNavigation][CL][1] | NOT AUTH | ANVL_C8R_Pisces_855968667336[855968667336]|CSCItemNavigation::OnQuantumDriveArrived|Quantum Drive has arrived at final destination"
    _, events = parse([sel.format("rs_ext_pyro2_l4"), arr, sel.format("PartyMemberMarker_781821676082"), arr])
    assert [e.get("x") for e in events if e["ti"] == "Quantum jump arrived"] == ["Checkmate", None]


def test_systems():
    assert t.system_of_code("RR_P3_LEO") == "Pyro" and t.system_of_code("Nyx_Kaboos") == "Nyx"
    assert t.system_of_code("RR_HUR_LEO") == "Stanton" and t.system_of_code("rr_jp_stantonpyro") == "Stanton"
    assert t.system_of_code("rs_ext_pyro-stan_jp1") == "Pyro" and t.system_of_code("somewhere") is None
    assert t.system_of_name("Orbituary") == "Pyro" and t.system_of_name("Attritus (Daymar)") == "Stanton"
    assert t.system_of_name("Rest stop Terminus (orbit)") == "Pyro" and t.system_of_name("Nyx System") == "Nyx"
