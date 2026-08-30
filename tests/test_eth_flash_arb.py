import unittest
from decimal import Decimal
import json
import os
from unittest.mock import patch
import sys
import tempfile
from pathlib import Path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
for sub in ("core", "recovery", "engines", "web", "deployers"):
    subpath = str(SRC_DIR / sub)
    if subpath not in sys.path:
        sys.path.insert(0, subpath)

from eth_flash_arb import (
    ArbError,
    MatchaClient,
    PYUSD,
    QuoteStaleError,
    StableClient,
    TransientRpcError,
    USDT,
    amount_to_raw,
    buffered_eth_usd_price,
    capacity_limited_loan_amount,
    classify_atomic_simulation_error,
    gas_cost_usdc_raw,
    minimum_output_after_slippage,
    parse_binance_eth_usdc_price,
    parse_stable_order,
    parse_stable_quote,
    parser,
    raw_to_amount,
    raw_to_signed_amount,
    select_best_matcha_quote,
    wei_cost_usdc_raw,
)
import eth_flash_arb_pyusd_usdc as pyusd_arb


TARGET = "0x1111111111111111111111111111111111111111"
ALLOWANCE_TARGET = "0x2222222222222222222222222222222222222222"


def matcha_response(buy_amount, simulation_result="success", include_data=True):
    quote = {
        "to": TARGET,
        "allowanceTarget": ALLOWANCE_TARGET,
        "sellAmount": "50000000000",
        "buyAmount": str(buy_amount),
        "value": "0",
        "gas": "245000",
        "gasPrice": "3000000000",
    }
    if include_data:
        quote["data"] = "0x12345678"
    return {
        "allowanceHolder": {
            "simulation": {"result": simulation_result},
            "quote": quote,
        }
    }


class EthereumFlashArbTests(unittest.TestCase):
    def test_pyusd_route_checksums_lowercase_matcha_addresses_for_web3(self):
        quote = pyusd_arb.MatchaQuote(
            aggregator="1inch",
            target="0x0000000000001ff3684f28c67538d4d072c22734",
            allowance_target="0x0000000000001ff3684f28c67538d4d072c22734",
            data="0x12345678",
            value=0,
            sell_amount=1_000_000,
            buy_amount=1_000_001,
        )

        Web3 = pyusd_arb.require_web3()
        arguments = pyusd_arb.checksum_matcha_arguments(Web3, quote)

        self.assertEqual(arguments[0], "0x0000000000001fF3684f28c67538d4D072C22734")
        self.assertTrue(Web3.is_checksum_address(arguments[0]))
        self.assertTrue(Web3.is_checksum_address(arguments[1]))

    def test_pyusd_order_uses_single_chain_create_endpoint(self):
        class RecordingHttp:
            def __init__(self):
                self.url = None

            def post(self, url, _payload, *, headers=None):
                self.url = url
                return None

        http = RecordingHttp()
        client = pyusd_arb.StableClient(http, "https://stable.invalid")
        with self.assertRaisesRegex(pyusd_arb.ArbError, "not an object"):
            client.create_order(TARGET, "PYUSD", "USDC", 1_000_000, 1_000_000)

        self.assertEqual(
            http.url,
            "https://stable.invalid/swap/create/singleChain",
        )

    def test_pyusd_engine_accepts_usdg_as_an_explicit_intermediate(self):
        configured = {
            "ETH_ARB_INTERMEDIATE_TOKEN": "USDG",
            "ETH_ARB_STABLECOIN_EXECUTOR": TARGET,
            "ETH_ARB_FLASH_PROVIDER": "uniswap-v4",
            "ETH_ARB_RECEIPT_TIMEOUT_SECONDS": "45",
        }
        with patch.dict(os.environ, configured, clear=False):
            args = pyusd_arb.parser().parse_args([])

        self.assertEqual(args.intermediate_token, "USDG")
        self.assertEqual(args.executor, TARGET)
        self.assertEqual(args.flash_provider, "uniswap-v4")
        self.assertEqual(args.receipt_timeout, 45.0)

    def test_flash_provider_auto_falls_back_to_aave_v3_for_usdg(self):
        provider = pyusd_arb.select_flash_provider(
            "auto",
            "USDG",
            100_000_000_000,
            {
                "morpho": 1_283_000,
                "aave-v3": 4_084_658_664_804,
            },
            {"aave-v3": 5},
        )

        self.assertEqual(provider.key, "aave-v3")
        self.assertEqual(provider.provider_id, 2)
        self.assertEqual(provider.available, 4_084_658_664_804)
        self.assertEqual(provider.premium_bps, 5)

    def test_flash_provider_auto_prefers_morpho_when_it_has_liquidity(self):
        provider = pyusd_arb.select_flash_provider(
            "auto",
            "PYUSD",
            100_000_000_000,
            {
                "morpho": 59_200_636_464_769,
                "uniswap-v4": 10_000_000_000_000,
            },
        )

        self.assertEqual(provider.key, "morpho")

    def test_aave_flash_fee_uses_protocol_half_up_rounding(self):
        self.assertEqual(pyusd_arb.flash_provider_fee(100_000_000_000, 5), 50_000_000)
        self.assertEqual(pyusd_arb.flash_provider_fee(1, 5), 0)

    def test_forced_morpho_fails_before_quote_when_usdg_is_unavailable(self):
        with self.assertRaisesRegex(
            pyusd_arb.ArbError,
            r"Morpho flash liquidity is 1\.283 USDG, below requested 100000 USDG",
        ):
            pyusd_arb.select_flash_provider(
                "morpho",
                "USDG",
                100_000_000_000,
                {"morpho": 1_283_000},
            )

    def test_matcha_gas_endpoint_price_field_is_supported(self):
        class StaticHttp:
            def get(self, *_args, **_kwargs):
                return {"price": "2099853516"}

        self.assertEqual(MatchaClient(StaticHttp()).gas_price(), 2_099_853_516)

    def test_clients_use_pyusd_to_usdt_to_pyusd_route(self):
        payload = StableClient._base_payload(
            TARGET,
            "USDT",
            "PYUSD",
            50_000_000_000,
        )
        self.assertEqual(payload["assetFrom"], "USDT")
        self.assertEqual(payload["assetTo"], "PYUSD")
        self.assertEqual(payload["amountFrom"], "50000")

        class RecordingHttp:
            def __init__(self):
                self.competition_payload = None

            def get(self, *_args, **_kwargs):
                return {"price": "2000000000"}

            def post(self, url, payload, **_kwargs):
                if url.endswith("/api/competitions"):
                    self.competition_payload = payload
                    return {"competitionId": "test"}
                return matcha_response(50_010_000_000)

        http = RecordingHttp()
        responses = MatchaClient(http).quotes(
            TARGET,
            50_000_000_000,
            1,
            ("0x",),
            sell_token_address=PYUSD,
            buy_token_address=USDT,
        )
        self.assertEqual(len(responses), 1)
        self.assertEqual(http.competition_payload["sellTokenAddress"], PYUSD.lower())
        self.assertEqual(http.competition_payload["buyTokenAddress"], USDT.lower())

    def test_parser_reads_route_configuration_from_environment(self):
        configured = {
            "ETH_EXECUTOR_ADDRESS": "0x3333333333333333333333333333333333333333",
            "ETH_OPERATOR_ADDRESS": "0x4444444444444444444444444444444444444444",
            "ETH_RPC_URL": "https://rpc.example",
            "ETH_ARB_AMOUNT": "12345.5",
            "ETH_ARB_LOAN_TOKEN": "pyusd",
            "ETH_ARB_STABLE_CAPACITY_BUFFER": "2.5",
            "ETH_ARB_SLIPPAGE_BPS": "7",
            "ETH_ARB_MIN_PROFIT": "2",
            "ETH_ARB_MIN_NET_PROFIT": "3",
            "ETH_ARB_ETH_USD": "3210.25",
            "ETH_ARB_ETH_PRICE_URL": "https://prices.example/ethusdc",
            "ETH_ARB_ETH_PRICE_BUFFER_BPS": "125",
            "ETH_ARB_HTTP_TIMEOUT_SECONDS": "11",
            "ETH_ARB_QUOTE_ATTEMPTS": "4",
            "ETH_ARB_RPC_TIMEOUT_SECONDS": "75",
            "ETH_ARB_GAS_LIMIT_MULTIPLIER": "1.30",
            "ETH_ARB_MAX_FEE_GWEI": "4.5",
            "ETH_ARB_AGGREGATORS": "0x,Bebop",
            "ETH_ARB_OUTPUT_PATH": "/tmp/test-plan.json",
        }
        with patch.dict(os.environ, configured):
            args = parser().parse_args([])
        self.assertEqual(args.amount, "12345.5")
        self.assertEqual(args.loan_token, "PYUSD")
        self.assertEqual(args.stable_capacity_buffer, "2.5")
        self.assertEqual(args.slippage_bps, 7)
        self.assertEqual(args.min_profit, "2")
        self.assertEqual(args.min_net_profit, "3")
        self.assertEqual(args.eth_usd, Decimal("3210.25"))
        self.assertEqual(args.eth_price_url, "https://prices.example/ethusdc")
        self.assertEqual(args.eth_price_buffer_bps, 125)
        self.assertEqual(args.timeout, 11.0)
        self.assertEqual(args.quote_attempts, 4)
        self.assertEqual(args.rpc_timeout, 75.0)
        self.assertEqual(args.gas_limit_multiplier, Decimal("1.30"))
        self.assertEqual(args.max_fee_gwei, Decimal("4.5"))
        self.assertEqual(args.aggregators, "0x,Bebop")
        self.assertEqual(args.output, "/tmp/test-plan.json")

    def test_six_decimal_amount_conversion_is_exact(self):
        self.assertEqual(amount_to_raw("50000.123456"), 50_000_123_456)
        self.assertEqual(raw_to_amount(50_000_123_456), "50000.123456")
        self.assertEqual(raw_to_amount(50_000_000_000), "50000")
        self.assertEqual(raw_to_signed_amount(-1_250_000), "-1.25")
        with self.assertRaises(ArbError):
            amount_to_raw("1.0000001")

    def test_matcha_minimum_output_applies_slippage_conservatively(self):
        self.assertEqual(
            minimum_output_after_slippage(50_055_876_871, 5),
            50_030_848_932,
        )
        self.assertEqual(minimum_output_after_slippage(1, 5), 0)

    def test_capacity_limit_scales_loan_to_fit_stable_input_pool(self):
        self.assertEqual(
            capacity_limited_loan_amount(
                100_000_000_000,
                100_102_720_092,
                53_056_160_000,
            ),
            53_001_716_587,
        )
        self.assertEqual(
            capacity_limited_loan_amount(
                50_000_000_000,
                49_990_000_000,
                53_056_160_000,
            ),
            50_000_000_000,
        )

    def test_binance_eth_price_is_parsed_and_buffered_upward(self):
        price = parse_binance_eth_usdc_price(
            {"symbol": "ETHUSDC", "price": "1857.25000000"}
        )
        self.assertEqual(price, Decimal("1857.25000000"))
        self.assertEqual(buffered_eth_usd_price(price, 100), Decimal("1875.83"))

    def test_rejects_invalid_binance_eth_price(self):
        with self.assertRaisesRegex(ArbError, "non-positive"):
            parse_binance_eth_usdc_price({"symbol": "ETHUSDC", "price": "0"})

    def test_classifies_matcha_insufficient_return_as_stale_quote(self):
        error = classify_atomic_simulation_error(
            ValueError("execution reverted: 0xe1caab11...064a4ec6")
        )
        self.assertIsInstance(error, QuoteStaleError)
        self.assertIn("minimum return", str(error))

    def test_classifies_provider_fee_repayment_shortfall(self):
        error = pyusd_arb.classify_atomic_simulation_error(
            ValueError("execution reverted: 0x4e88422a")
        )
        self.assertIsInstance(error, pyusd_arb.QuoteStaleError)
        self.assertIn("provider fee", str(error))

    def test_classifies_nested_uniswap_v4_unlock_conflict(self):
        error = pyusd_arb.classify_atomic_simulation_error(
            ValueError("execution reverted: outer...5090d6c6")
        )
        self.assertIs(type(error), pyusd_arb.ArbError)
        self.assertIn("same PoolManager", str(error))

    def test_preserves_unknown_simulation_error(self):
        error = classify_atomic_simulation_error(ValueError("execution reverted: 0xdeadbeef"))
        self.assertIs(type(error), ArbError)
        self.assertIn("0xdeadbeef", str(error))

    def test_classifies_rpc_timeout_as_transient(self):
        error = classify_atomic_simulation_error(
            TimeoutError("HTTPSConnectionPool: Read timed out")
        )
        self.assertIsInstance(error, TransientRpcError)

    def test_selects_best_successful_executable_matcha_quote(self):
        quote = select_best_matcha_quote(
            [
                ("0x", matcha_response(50_010_000_000)),
                ("Bebop", matcha_response(50_011_000_000)),
                ("BadSimulation", matcha_response(60_000_000_000, "reverted")),
                ("NoCalldata", matcha_response(70_000_000_000, include_data=False)),
            ],
            50_000_000_000,
        )
        self.assertEqual(quote.aggregator, "Bebop")
        self.assertEqual(quote.buy_amount, 50_011_000_000)
        self.assertEqual(quote.contract_tuple()[4], bytes.fromhex("12345678"))

    def test_rejects_matcha_quote_for_different_sell_amount(self):
        response = matcha_response(50_010_000_000)
        response["allowanceHolder"]["quote"]["sellAmount"] = "1"
        with self.assertRaisesRegex(ArbError, "sell amount changed"):
            select_best_matcha_quote([("0x", response)], 50_000_000_000)

    def test_parses_stable_quote_in_human_six_decimal_units(self):
        quote = parse_stable_quote(
            {
                "data": {
                    "amountTo": "49975",
                    "fees": {"tokenFee": "25"},
                    "asset": {"available": "203153.55"},
                }
            },
            50_000_000_000,
        )
        self.assertEqual(quote.amount_in, 50_000_000_000)
        self.assertEqual(quote.amount_out, 49_975_000_000)
        self.assertEqual(quote.token_fee, 25_000_000)
        self.assertEqual(quote.capacity, Decimal("203153.55"))

    def test_parses_current_stable_balance_as_input_capacity(self):
        quote = parse_stable_quote(
            {
                "asset": {
                    "asset": "PYUSD",
                    "address": PYUSD,
                    "balance": "53056.16",
                    "min": "1000",
                    "max": "1000000",
                    "amountFrom": "50000",
                    "amountTo": "49990",
                }
            },
            50_000_000_000,
        )
        self.assertEqual(quote.capacity, Decimal("53056.16"))
        self.assertEqual(quote.minimum, Decimal("1000"))
        self.assertEqual(quote.maximum, Decimal("1000000"))

    def test_stable_quote_fractional_raw_output_rounds_down(self):
        quote = parse_stable_quote(
            {
                "asset": {
                    "amountTo": "50032.9650879575",
                    "tokenFee": "25.0289970425",
                }
            },
            50_057_994_085,
        )
        self.assertEqual(quote.amount_out, 50_032_965_087)
        self.assertEqual(quote.token_fee, 25_028_998)

    def test_parses_ethereum_stable_order(self):
        order = parse_stable_order(
            {
                "data": {
                    "nonce": "7",
                    "deadline": "9999999999",
                    "maintainerSignature": "0x" + "11" * 65,
                    "executionFeeNative": "0",
                    "orderId": "order-123",
                }
            },
            50_010_000_000,
        )
        self.assertEqual(order.nonce, 7)
        self.assertEqual(order.amount_in, 50_010_000_000)
        self.assertEqual(len(order.contract_tuple()[3]), 65)

    def test_rejects_non_ethereum_signature_length(self):
        with self.assertRaisesRegex(ArbError, "65 bytes"):
            parse_stable_order(
                {
                    "nonce": "1",
                    "deadline": "9999999999",
                    "maintainerSignature": "0x" + "11" * 64,
                },
                1_000_000,
            )

    def test_conservative_gas_cost_rounds_up(self):
        # 400,000 gas * 10 gwei = 0.004 ETH; at $3,000 this is 12 USDC.
        self.assertEqual(
            gas_cost_usdc_raw(400_000, 10_000_000_000, Decimal("3000")),
            12_000_000,
        )
        self.assertEqual(
            wei_cost_usdc_raw(4_000_000_000_000_000, Decimal("3000")),
            12_000_000,
        )

    def test_broadcast_is_confirmed_only_after_successful_receipt(self):
        class FakeHash:
            def hex(self):
                return "abc123"

        class FakeEth:
            def wait_for_transaction_receipt(self, *_args, **_kwargs):
                return {
                    "status": 1,
                    "blockNumber": 123,
                    "gasUsed": 456,
                    "effectiveGasPrice": 789,
                }

        plan = {}
        pyusd_arb.record_transaction_receipt(
            plan,
            type("FakeWeb3", (), {"eth": FakeEth()})(),
            FakeHash(),
            10,
        )

        self.assertEqual(plan["transactionStatus"], "confirmed")
        self.assertEqual(plan["transactionHash"], "0xabc123")
        self.assertEqual(plan["transactionReceipt"]["status"], 1)

    def test_receipt_timeout_remains_ambiguous_not_successful(self):
        TimeExhausted = type("TimeExhausted", (Exception,), {})

        class FakeHash:
            def hex(self):
                return "def456"

        class FakeEth:
            def wait_for_transaction_receipt(self, *_args, **_kwargs):
                raise TimeExhausted()

        plan = {}
        with tempfile.TemporaryDirectory() as temporary:
            output = str(Path(temporary) / "plan.json")
            pyusd_arb.record_transaction_receipt(
                plan,
                type("FakeWeb3", (), {"eth": FakeEth()})(),
                FakeHash(),
                10,
                output,
            )
            persisted = json.loads(Path(output).read_text())

        self.assertEqual(plan["transactionStatus"], "submitted")
        self.assertEqual(plan["receiptConfirmation"], "timed-out")
        self.assertEqual(persisted["transactionStatus"], "submitted")


if __name__ == "__main__":
    unittest.main()
