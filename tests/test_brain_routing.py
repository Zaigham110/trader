"""D-047: the cycle stamps proposals and intents with the routed bundle and gates with its variant."""

from goldtrader.config.schema import StrategyConfig
from tests.test_brain_cycle import GOOD_PROPOSAL, RISK_OK, STRAT, FakeGateway, _cycle, _rows, state


def test_router_stamps_bundle_and_uses_variant():
    variant = StrategyConfig(
        id=STRAT.id,
        kind=STRAT.kind,
        enabled=True,
        params={**STRAT.params, "stop_atr": 0.8},
        stop_atr_min=0.3,
        stop_atr_max=1.2,
        min_reward_risk=1.0,
    )
    routed = []

    def router(proposal_id):
        routed.append(proposal_id)
        return "cand-123", variant

    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw, router=router)
    fake.script("fast", [RISK_OK])
    fake.script("standard", [GOOD_PROPOSAL])
    res = cyc.run_once()
    assert res.status == "sent" and res.bundle_id == "cand-123" and routed == [res.proposal_id]
    assert gw.intents[0].bundle_id == "cand-123"
    (prop,) = _rows(conn, "proposals")
    assert prop["bundle_id"] == "cand-123"


def test_router_failure_falls_back_to_champion():
    def router(_pid):
        raise RuntimeError("router down")

    gw = FakeGateway(state())
    cyc, reg, fake, conn, _ = _cycle(gw, router=router)
    fake.script("fast", [RISK_OK])
    fake.script("standard", [GOOD_PROPOSAL])
    res = cyc.run_once()
    assert res.status == "sent" and res.bundle_id == STRAT.id
    assert any(
        s.get("error", "").startswith("RuntimeError") for s in res.steps if s["step"] == "route"
    )
