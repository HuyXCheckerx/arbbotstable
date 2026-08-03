import os
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

from multichain_flash_arb import (
    CHAINS,
    MatchaClient,
    StableClient,
    amount_to_raw,
    capacity_limited_loan_amount,
    flash_fee_raw,
    parse_matcha_quote,
    parse_stable_order,
    parse_stable_quote,
    parser,
)


class RecordingHttp:
    def __init__(self):
        self.calls = []

    def post(self, url, payload, *, headers=None):
        self.calls.append((url, payload, headers))
        return {"amountTo": "1"}


class MultichainFlashArbTests(unittest.TestCase):
    def test_chain_configuration_matches_supported_assets_and_lenders(self):
        polygon = CHAINS["polygon"]
        self.assertEqual(polygon.chain_id, 137)
        self.assertEqual(polygon.stable_chain_id, "106")
        self.assertEqual(polygon.decimals, 6)
        self.assertEqual(polygon.provider_kind, 0)
        self.assertEqual(
            polygon.flash_lender.lower(),
            "0x1bf0c2541f820e775182832f06c0b7fc27a25f67",
        )

        bsc = CHAINS["bsc"]
        self.assertEqual(bsc.chain_id, 56)
        self.assertEqual(bsc.stable_chain_id, "103")
        self.assertEqual(bsc.decimals, 18)
        self.assertEqual(bsc.provider_kind, 1)
        self.assertEqual(
            bsc.flash_lender.lower(),
            "0x6807dc923806fe8fd134338eabca509979a7e0cb",
        )

    def test_amount_conversion_respects_each_chains_token_decimals(self):
        self.assertEqual(amount_to_raw("100000", CHAINS["polygon"].decimals), 10**11)
        self.assertEqual(amount_to_raw("100000", CHAINS["bsc"].decimals), 10**23)

    def test_aave_premium_is_subtracted_but_morpho_is_zero_fee(self):
        polygon_amount = amount_to_raw("100000", 6)
        bsc_amount = amount_to_raw("100000", 18)
        self.assertEqual(flash_fee_raw(polygon_amount, 0), 0)
        self.assertEqual(
            flash_fee_raw(bsc_amount, 5),
            amount_to_raw("50", 18),
        )

    def test_stable_payload_uses_internal_chain_ids_and_human_amounts(self):
        executor = "0x" + "11" * 20
        for key, expected_chain in (("polygon", "106"), ("bsc", "103")):
            http = RecordingHttp()
            client = StableClient(http, CHAINS[key], "https://stable.invalid")
            client.quote(executor, amount_to_raw("100000", CHAINS[key].decimals))
            payload = http.calls[0][1]
            self.assertEqual(payload["chainFrom"], expected_chain)
            self.assertEqual(payload["chainTo"], expected_chain)
            self.assertEqual(payload["amountFrom"], "100000")
            self.assertEqual(payload["assetFrom"], "USDT")
            self.assertEqual(payload["assetTo"], "USDC")

    def test_matcha_payload_uses_evm_chain_and_token_decimals(self):
        class MatchaHttp:
            def __init__(self):
                self.posts = []

            def get(self, _url, *, headers=None):
                return {"price": "1000000000"}

            def post(self, url, payload, *, headers=None):
                self.posts.append((url, payload))
                if url.endswith("/api/competitions"):
                    return {"competitionId": "competition"}
                return {
                    "allowanceHolder": {
                        "simulation": {"result": "success"},
                        "quote": {
                            "allowanceTarget": "0x" + "22" * 20,
                            "sellAmount": str(amount_to_raw("1", 18)),
                            "buyAmount": str(amount_to_raw("1.001", 18)),
                            "transaction": {
                                "to": "0x" + "33" * 20,
                                "data": "0x12345678",
                                "value": "0",
                            },
                        },
                    }
                }

        http = MatchaHttp()
        client = MatchaClient(http, CHAINS["bsc"], "https://matcha.invalid")
        sell_amount = amount_to_raw("1", 18)
        client.quotes("0x" + "11" * 20, sell_amount, 5, ("0x",))
        competition = http.posts[0][1]
        self.assertEqual(competition["chainId"], 56)
        self.assertEqual(competition["sellTokenDecimals"], 18)
        self.assertEqual(competition["buyTokenDecimals"], 18)
        self.assertEqual(competition["sellAmount"], str(sell_amount))

    def test_parses_bsc_stable_quote_in_human_18_decimal_units(self):
        amount_in = amount_to_raw("100087.123456789012345678", 18)
        quote = parse_stable_quote(
            {
                "amountTo": "100037.079895060617839506",
                "tokenFee": "50.043561728394506172",
                "balance": "70000.01",
            },
            amount_in,
            18,
        )
        self.assertEqual(
            quote.amount_out,
            amount_to_raw("100037.079895060617839506", 18),
        )
        self.assertEqual(
            quote.token_fee,
            amount_to_raw("50.043561728394506172", 18),
        )
        self.assertEqual(quote.capacity, Decimal("70000.01"))

    def test_matcha_parser_preserves_exact_raw_18_decimal_amounts(self):
        sell = amount_to_raw("100000", 18)
        buy = amount_to_raw("100087.123456789012345678", 18)
        quote = parse_matcha_quote(
            "0x",
            {
                "allowanceHolder": {
                    "simulation": {"result": "success"},
                    "quote": {
                        "allowanceTarget": "0x" + "22" * 20,
                        "sellAmount": str(sell),
                        "buyAmount": str(buy),
                        "transaction": {
                            "to": "0x" + "33" * 20,
                            "data": "0x12345678",
                            "value": "0",
                        },
                    },
                }
            },
            sell,
        )
        self.assertEqual(quote.sell_amount, sell)
        self.assertEqual(quote.buy_amount, buy)

    def test_parses_evm_stable_order_at_bsc_precision(self):
        amount_in = amount_to_raw("1000.123456789012345678", 18)
        order = parse_stable_order(
            {
                "data": {
                    "amountFrom": "1000.123456789012345678",
                    "deadline": int(time.time()) + 300,
                    "nonce": "42",
                    "maintainerSignature": "0x" + "11" * 65,
                    "executionFeeNative": "123",
                }
            },
            amount_in,
            18,
        )
        self.assertEqual(order.amount_in, amount_in)
        self.assertEqual(order.nonce, 42)
        self.assertEqual(order.execution_fee_native, 123)

    def test_capacity_sizing_scales_flash_principal(self):
        loan = amount_to_raw("100000", 18)
        stable_input = amount_to_raw("100087", 18)
        capacity = amount_to_raw("70000", 18)
        adjusted = capacity_limited_loan_amount(loan, stable_input, capacity)
        self.assertLess(adjusted, loan)
        self.assertEqual(adjusted, loan * capacity // stable_input)

    def test_parser_reads_selected_chain_environment_only(self):
        environment = {
            "EVM_ARB_CHAIN": "bsc",
            "BSC_RPC_URL": "https://bsc.invalid",
            "BSC_OPERATOR_ADDRESS": "0x" + "44" * 20,
            "BSC_EXECUTOR_ADDRESS": "0x" + "55" * 20,
            "BSC_ARB_AMOUNT_USDC": "1234",
            "POLYGON_RPC_URL": "https://polygon.invalid",
        }
        with patch.dict(os.environ, environment, clear=False):
            args = parser([]).parse_args([])
        self.assertEqual(args.chain, "bsc")
        self.assertEqual(args.rpc_url, "https://bsc.invalid")
        self.assertEqual(args.amount, "1234")
        self.assertEqual(args.operator, "0x" + "44" * 20)
        self.assertEqual(args.executor, "0x" + "55" * 20)


if __name__ == "__main__":
    unittest.main()
