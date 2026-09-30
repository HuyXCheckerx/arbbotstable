"""Read-only payout-vault monitoring. Balances trigger rechecks, never authorize trades."""
import time
import requests

ETH_POOL = '0xCfC1bc6013eD89D484c626dd9ee5EB7bc1a1d9Da'
ETH_TOKENS = {
    'USDC': '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48',
    'USDG': '0xe343167631d89b6ffc58b88d6b7fb0228795491d',
    'PYUSD': '0x6c3ea9036406852006290770bedfcaba0e23a0e8',
}
SOL_MINTS = {
    'USDC': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
    'USDG': '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH',
    'PYUSD': '2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo',
}


def solana_vault(symbol):
    from solders.pubkey import Pubkey
    mint = Pubkey.from_string(SOL_MINTS[symbol])
    pool, _ = Pubkey.find_program_address([b'pool', bytes(mint)],
        Pubkey.from_string('2zz7bEA4TzSJFvvGBgdVAdFBpAfkZHK3fCFBQk63MiBG'))
    token_program = Pubkey.from_string('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA' if symbol == 'USDC'
        else 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb')
    vault, _ = Pubkey.find_program_address([bytes(pool), bytes(token_program), bytes(mint)],
        Pubkey.from_string('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'))
    return str(vault)


class StableLiquidityMonitor:
    def __init__(self, chain, rpc_url, poll_seconds=2.0, retry_seconds=10.0):
        self.chain, self.rpc_url = chain, rpc_url
        self.poll_seconds, self.retry_seconds = poll_seconds, retry_seconds
        self.next_poll = 0.0
        self.balances = {}
        self.waiting = {}

    def _read(self):
        symbols = sorted({entry['token'] for entry in self.waiting.values()})
        if not symbols or not self.rpc_url:
            return {}
        if self.chain == 'ethereum':
            calls = [{'jsonrpc': '2.0', 'id': i, 'method': 'eth_call', 'params': [
                {'to': ETH_TOKENS[s], 'data': '0x70a08231' + ETH_POOL[2:].lower().zfill(64)}, 'latest']}
                for i, s in enumerate(symbols)]
        else:
            calls = [{'jsonrpc': '2.0', 'id': i, 'method': 'getTokenAccountBalance',
                      'params': [solana_vault(s), {'commitment': 'confirmed'}]}
                     for i, s in enumerate(symbols)]
        response = requests.post(self.rpc_url, json=calls, timeout=3)
        response.raise_for_status()
        result = {}
        for item in response.json():
            if 'result' not in item:
                continue
            symbol = symbols[int(item['id'])]
            result[symbol] = (int(item['result'], 16) if self.chain == 'ethereum'
                              else int(item['result']['value']['amount']))
        return result

    def arm(self, route):
        # Payout is the token Stable sends, independent of the flash-loan token.
        self.waiting[route.key] = {'token': route.stable_to,
            'baseline': self.balances.get(route.stable_to), 'retry': time.monotonic() + self.retry_seconds}

    def clear(self, route):
        self.waiting.pop(route.key, None)

    def poll(self):
        now = time.monotonic()
        if not self.waiting or now < self.next_poll:
            return
        self.next_poll = now + self.poll_seconds
        try:
            self.balances = self._read()
        except Exception:
            # Never interpret an RPC failure as zero liquidity. Periodic full
            # checks still run, using the engine's RPC recovery and safety checks.
            self.balances = {}
        for entry in self.waiting.values():
            if entry['baseline'] is None:
                entry['baseline'] = self.balances.get(entry['token'])

    def due(self, route):
        entry = self.waiting.get(route.key)
        if entry is None:
            return True
        balance = self.balances.get(entry['token'])
        return (time.monotonic() >= entry['retry'] or
                balance is not None and entry['baseline'] is not None and balance > entry['baseline'])
