import unittest
from decimal import Decimal

from scripts.crosschain_pyusd_usdg_monitor import (
    SOLANA_USDG_MINT,
    StableCrosschainQuote,
    build_stable_quote_payload,
    discover_usdg_oft,
    evaluate_route,
    stable_crosschain_quote,
)


ETH_ADDRESS = "0x000000000000000000000000000000000000dEaD"
SOL_ADDRESS = "11111111111111111111111111111111"


class FakeHttp:
    def __init__(self, *, get_payload=None, post_payload=None):
        self.get_payload = get_payload
        self.post_payload = post_payload
        self.last_url = None
        self.last_post = None

    def get(self, url, *, headers=None):
        self.last_url = url
        return self.get_payload

    def post(self, url, payload, *, headers=None):
        self.last_url = url
        self.last_post = payload
        return self.post_payload


class CrosschainMonitorTests(unittest.TestCase):
    def test_builds_eth_pyusd_to_solana_usdg_status_request(self):
        payload = build_stable_quote_payload(ETH_ADDRESS, SOL_ADDRESS, 1_000_000_001)

        self.assertEqual(payload["chainFrom"], "101")
        self.assertEqual(payload["assetFrom"], "PYUSD")
        self.assertEqual(payload["chainTo"], "102")
        self.assertEqual(payload["assetTo"], "USDG")
        self.assertEqual(payload["amountFrom"], "1000.000001")
        self.assertFalse(payload["gasLess"])

    def test_parses_crosschain_quote_and_known_fees(self):
        http = FakeHttp(
            post_payload={
                "asset": {
                    "amountFrom": "1000",
                    "amountTo": "1000",
                    "balance": 99998.1,
                    "min": 1000,
                    "max": 1000000,
                    "nativeFeeUsd": "0.05",
                    "executionFeeUSD": "0.14",
                    "tokenFee": 0,
                }
            }
        )

        quote = stable_crosschain_quote(
            http, "https://stable.invalid", ETH_ADDRESS, SOL_ADDRESS, 1_000_000_000
        )

        self.assertEqual(quote.amount_out_raw, 1_000_000_000)
        self.assertEqual(quote.reported_capacity, Decimal("99998.1"))
        self.assertEqual(quote.known_fee_usd, Decimal("0.19"))

    def test_accepts_only_canonical_usdg_oft_metadata(self):
        http = FakeHttp(
            get_payload={
                "USDG": [
                    {
                        "sharedDecimals": 6,
                        "deployments": {
                            "solana": {"address": SOLANA_USDG_MINT},
                            "ethereum": {
                                "address": "0x147bde4f997f0d4c7544ed0c55eacf1e5e6bf9c4",
                                "innerTokenAddress": "0xe343167631d89B6Ffc58B88d6b7fB0228795491D",
                            },
                        },
                    }
                ]
            }
        )

        deployment = discover_usdg_oft(http, "https://layerzero.invalid")

        self.assertEqual(deployment.shared_decimals, 6)
        self.assertEqual(deployment.solana_address, SOLANA_USDG_MINT)

    def test_positive_edge_needs_bridge_fee_before_net_signal(self):
        stable = StableCrosschainQuote(
            amount_in_raw=1_000_000_000,
            amount_out_raw=1_000_000_000,
            reported_capacity=Decimal("5000"),
            minimum=Decimal("1000"),
            maximum=Decimal("1000000"),
            native_fee_usd=Decimal("0.05"),
            execution_fee_usd=Decimal("0.14"),
            token_fee_raw=0,
        )

        state, _, gross, _, net = evaluate_route(
            reserve=Decimal("5001.9"),
            reserve_floor=Decimal("1.9"),
            amount_in_raw=1_000_000_000,
            stable=stable,
            matcha_output_raw=1_002_000_000,
            matcha_gas=100_000,
            matcha_gas_price=1_000_000_000,
            eth_price=Decimal("2000"),
            bridge_fee_usd=None,
            min_net_profit=Decimal("1"),
        )

        self.assertEqual(state, "REVIEW_BRIDGE_FEE")
        self.assertEqual(gross, Decimal("2"))
        self.assertIsNone(net)

    def test_reserve_floor_blocks_quote_even_with_positive_edge(self):
        stable = StableCrosschainQuote(
            amount_in_raw=1_000_000_000,
            amount_out_raw=1_000_000_000,
            reported_capacity=Decimal("1000"),
            minimum=Decimal("1000"),
            maximum=Decimal("1000000"),
            native_fee_usd=Decimal(0),
            execution_fee_usd=Decimal(0),
            token_fee_raw=0,
        )

        state, _, _, _, _ = evaluate_route(
            reserve=Decimal("1001.8"),
            reserve_floor=Decimal("1.9"),
            amount_in_raw=1_000_000_000,
            stable=stable,
            matcha_output_raw=1_010_000_000,
            matcha_gas=None,
            matcha_gas_price=None,
            eth_price=Decimal("2000"),
            bridge_fee_usd=Decimal(0),
            min_net_profit=Decimal("1"),
        )

        self.assertEqual(state, "WAIT_RESERVE")

    def test_fee_complete_quote_must_clear_net_floor(self):
        stable = StableCrosschainQuote(
            amount_in_raw=1_000_000_000,
            amount_out_raw=1_000_000_000,
            reported_capacity=Decimal("5000"),
            minimum=Decimal("1000"),
            maximum=Decimal("1000000"),
            native_fee_usd=Decimal("0.1"),
            execution_fee_usd=Decimal("0.1"),
            token_fee_raw=0,
        )

        state, _, _, _, net = evaluate_route(
            reserve=Decimal("5001.9"),
            reserve_floor=Decimal("1.9"),
            amount_in_raw=1_000_000_000,
            stable=stable,
            matcha_output_raw=1_002_000_000,
            matcha_gas=100_000,
            matcha_gas_price=1_000_000_000,
            eth_price=Decimal("2000"),
            bridge_fee_usd=Decimal("0.25"),
            min_net_profit=Decimal("1"),
        )

        self.assertEqual(state, "MANUAL_REVIEW")
        self.assertEqual(net, Decimal("1.35"))


if __name__ == "__main__":
    unittest.main()
