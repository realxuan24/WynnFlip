"""Trigger one real WynnFlip backend scan from GitHub Actions.

The deployed Vercel backend writes both price and listing-lifecycle history to
Upstash Redis. This caller does not carry credentials or modify GitHub files.
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get('WYNNFLIP_MARKET_URL', 'https://wynn-flip.vercel.app/api/market')


def check_payload(data):
    if not isinstance(data, dict) or data.get('error'):
        raise ValueError('Backend returned an error')
    # Do not mistake a successful HTTP response for a persisted observation.
    diagnostic = data.get('status')
    if not isinstance(diagnostic, dict):
        raise ValueError('Missing backend status: cannot verify cloud history write')
    if diagnostic.get('history_write_error') or diagnostic.get('lifecycle_write_error'):
        raise ValueError('Cloud history/lifecycle write reported failure')
    if diagnostic.get('persistent_history') is not True:
        raise ValueError('Backend did not verify persistent Upstash history')
    if diagnostic.get('lifecycle_available') is not True:
        raise ValueError('Backend did not verify lifecycle storage')
    return diagnostic


def run():
    request = urllib.request.Request(URL, headers={'Accept': 'application/json', 'User-Agent': 'WynnFlip-Cloud-History/1.0'})
    with urllib.request.urlopen(request, timeout=105) as response:
        payload = json.load(response)
    d = check_payload(payload)
    print('Cloud history updated successfully.')
    print('History count:', d.get('durable_history_offers', 'unknown'))
    print('Tracked listing IDs:', d.get('lifecycle_tracked_listings', 'unknown'))
    print('Eligible IDs:', d.get('lifecycle_eligible_scan_ids', 'unknown'))
    print('Last scan:', d.get('updated_at', 'unknown'))


if __name__ == '__main__':
    try:
        run()
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print('Scheduled scan failed:', str(exc)[:200], file=sys.stderr)
        sys.exit(1)
