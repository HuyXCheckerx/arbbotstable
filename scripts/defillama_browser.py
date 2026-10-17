"""Read quotes produced by LlamaSwap's normal, unconnected-wallet browser UI."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

SITE = 'https://swap.defillama.com/'


def displayed_quotes(text, buy, protocols=None):
    """Parse only visible route cards; displayed amounts are indicative, rounded quotes."""
    if not isinstance(text, str) or 'Select a route to perform a swap' not in text:
        return []
    routes = text.split('Select a route to perform a swap', 1)[1].split('FAQ', 1)[0]
    pattern = re.compile(
        r'(?m)^([\d,]+(?:\.\d+)?)\s*\n\s*' + re.escape(buy)
        + r'\s*\n'
    )
    observed_at = datetime.now(timezone.utc).isoformat()
    results = []
    cards = list(pattern.finditer(routes))
    for index, match in enumerate(cards):
        end = cards[index + 1].start() if index + 1 < len(cards) else len(routes)
        details = routes[match.end():end]
        provider = re.search(r'(?m)^via\s+([^\n]+)', details)
        if not provider:
            continue
        protocol = provider[1].strip()
        if protocols and protocol not in protocols:
            continue
        amount = Decimal(match[1].replace(',', ''))
        if not amount.is_finite() or amount <= 0:
            continue
        gas = re.search(r'(?m)^\$([\d,]+(?:\.\d+)?)\s*$', details)
        results.append({
            'protocol': protocol, 'status': 'quoted',
            'amount_out': format(amount, 'f'),
            'source': 'website_display', 'precision': 'displayed',
            'executable': False, 'observed_at': observed_at,
            'quote_timestamp_available': False,
            'verified_by_website': 'VERIFIED' in details,
            'gas_usd': gas[1].replace(',', '') if gas else None,
            'refreshing': 'Refreshing quote' in details,
        })
    return results


def quote_identity(url):
    parsed = urlsplit(url)
    if parsed.hostname != 'swap-api.defillama.com' or parsed.path != '/dexAggregatorQuote':
        return None
    query = parse_qs(parsed.query)
    return {k: query.get(k, [''])[0] for k in ('protocol', 'from', 'to', 'amount')}


def quote_result(protocol, status, headers, data):
    result = {'protocol': protocol, 'status': 'error', 'http_status': status,
              'updated_at': datetime.now(timezone.utc).isoformat()}
    if headers.get('cf-mitigated') == 'challenge':
        return {**result, 'error': 'Cloudflare challenged this browser session; no quote received'}
    if status != 200:
        return {**result, 'error': f'Quote endpoint returned HTTP {status}'}
    try:
        amount = Decimal(str(data.get('amountReturned')))
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise InvalidOperation
    except (AttributeError, InvalidOperation, ValueError):
        return {**result, 'status': 'no_quote', 'error': 'Endpoint returned no valid output amount'}
    # Intentionally whitelist fields: provider data can contain authenticated URLs.
    return {**result, 'status': 'quoted', 'amount_out_raw': str(int(amount)),
            'amount_out': format(amount / 1_000_000, 'f')}


class BrowserQuotes:
    def __init__(self, *, tokens, sell, buy, amount, slippage, protocols, state_path,
                 headful=False, hide_ip=False, channel=None):
        self.tokens, self.sell, self.buy = tokens, sell, buy
        self.amount, self.slippage, self.protocols = amount, slippage, protocols
        self.state_path, self.headful = Path(state_path), headful
        self.hide_ip, self.channel = hide_ip, channel
        self.results = {}
        self.pending = set()
        self.responses = []
        self.pw = self.browser = self.context = None

    def identity(self, url):
        identity = quote_identity(url)
        if (identity and (not self.protocols or identity['protocol'] in self.protocols)
                and identity['from'].lower() == self.tokens[self.sell].lower()
                and identity['to'].lower() == self.tokens[self.buy].lower()
                and identity['amount'] == str(int(Decimal(self.amount) * 1_000_000))):
            return identity['protocol']
        return None

    def request(self, request):
        protocol = self.identity(request.url)
        if protocol:
            self.pending.add(protocol)

    def response(self, response):
        if self.identity(response.url):
            self.responses.append(response)

    def failed(self, request):
        protocol = self.identity(request.url)
        if protocol:
            self.pending.discard(protocol)
            self.results[protocol] = {'protocol': protocol, 'status': 'error',
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'error': 'Browser request failed (network/CORS); no HTTP response accessible'}

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        try:
            self.pw = sync_playwright().start()
            launch = {
                'headless': not self.headful,
                'args': [
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-infobars',
                    '--disable-dev-shm-usage',
                    '--no-proxy-server',
                ],
            }
            if self.channel:
                launch['channel'] = self.channel
            self.browser = self.pw.chromium.launch(**launch)
            options = {
                'viewport': {'width': 1280, 'height': 800},
                'user_agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/126.0.0.0 Safari/537.36'
                ),
            }
            if self.state_path.exists():
                options['storage_state'] = str(self.state_path)
            self.context = self.browser.new_context(**options)
            self.page = self.context.new_page()
            self.page.on('request', self.request)
            self.page.on('response', self.response)
            self.page.on('requestfailed', self.failed)
            self.page.goto(SITE + '?' + urlencode({'chain': 'ethereum',
                'from': self.tokens[self.sell], 'to': self.tokens[self.buy]}),
                wait_until='domcontentloaded', timeout=60000)
            privacy = self.page.locator('#privacy-switch')
            privacy.wait_for(timeout=60000)
            if privacy.is_checked() != self.hide_ip:
                self.page.locator('label').filter(has=privacy).click()
            self.page.get_by_placeholder('Custom', exact=True).fill(str(self.slippage))
            self.page.get_by_placeholder('0', exact=True).first.fill(self.amount)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def snapshot(self, seconds):
        if seconds > 0:
            try:
                self.page.wait_for_timeout(seconds * 1000)
            except Exception:
                pass
        responses, self.responses = self.responses, []
        for response in responses:
            protocol = self.identity(response.url)
            try:
                data = response.json()
            except Exception:
                data = None
            self.results[protocol] = quote_result(protocol, response.status, response.headers, data)
            self.pending.discard(protocol)
        if not self.hide_ip:
            # Direct provider APIs use different response formats. Reading the
            # rendered cards also avoids exporting the site's integration keys.
            try:
                text = self.page.locator('body').inner_text()
            except Exception:
                text = ""
            visible = displayed_quotes(text, self.buy, self.protocols)
            if visible:
                return sorted(visible, key=lambda q: Decimal(q['amount_out']), reverse=True)
            # Never carry a disappeared DOM quote into a later report.
            return [{'protocol': protocol, 'status': 'pending',
                     'error': 'No visible route quote; website may be loading or blocked'}
                    for protocol in (self.protocols or ['all'])]
        return [{**self.results.get(protocol, {'protocol': protocol, 'status': 'pending',
                    'error': 'No quote response received yet'}),
                 'refreshing': protocol in self.pending} for protocol in (self.protocols or self.results)]

    def __exit__(self, *_):
        try:
            if self.context:
                try:
                    self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    state = self.context.storage_state()
                    fd = os.open(self.state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, 'w') as stream:
                        json.dump(state, stream)
                    os.chmod(self.state_path, 0o600)
                except Exception:
                    pass
        finally:
            try:
                if self.context:
                    self.context.close()
            except Exception:
                pass
            try:
                if self.browser:
                    self.browser.close()
            except Exception:
                pass
            try:
                if self.pw:
                    self.pw.stop()
            except Exception:
                pass
