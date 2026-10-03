"""Live HTTP correctness checks, then a configurable hot-seat stampede."""
import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sys
import ssl
import time
from uuid import uuid4

import httpx


async def run(base_url, admin_token, requests, concurrency):
    tls = ssl.create_default_context()
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base_url.rstrip('/'), timeout=180, limits=limits, verify=tls) as client:
        admin = {'Authorization': f'Bearer {admin_token}'}

        async def token(uid, transport=None):
            r = await (transport or client).post('/auth/tokens', headers=admin, json={'user_id': uid})
            r.raise_for_status()
            return {'Authorization': 'Bearer '+r.json()['access_token']}

        async def show(labels, limit=4):
            r = await client.post('/shows', headers=admin,
                json={'name': 'burst-'+str(uuid4()), 'seats': labels, 'price_paise': 25000, 'per_user_limit': limit})
            assert r.status_code == 201, r.text
            return r.json()['id']

        async def reserve(sid, labels, headers, key=None, transport=None, **extra):
            return await (transport or client).post(f'/shows/{sid}/reserve', headers=headers,
                json={'seats': labels, 'idempotency_key': key or str(uuid4()), **extra})

        async def state(sid):
            r = await client.get(f'/shows/{sid}')
            r.raise_for_status()
            data = r.json()
            c = data['counts']
            assert c['available']+c['held']+c['confirmed'] == c['total_seats'], data
            observed = Counter(s['status'] for s in data['seats'])
            assert all(observed[k] == c[k] for k in ('available', 'held', 'confirmed')), data
            return data

        alice, bob = await asyncio.gather(token('alice-'+str(uuid4())), token('bob-'+str(uuid4())))
        sid = await show([f'A{i}' for i in range(12)])
        replies = await asyncio.gather(*(reserve(sid,[f'A{i}'],alice) for i in range(10)))
        assert Counter(r.status_code for r in replies) == {201: 4, 409: 6}
        assert all(r.json()['error']=='per_user_limit' for r in replies if r.status_code == 409)
        assert (await state(sid))['counts']['confirmed'] == 4

        sid = await show(['A','B','C'])
        key = str(uuid4())
        replies = await asyncio.gather(*(reserve(sid,['A'],alice,key,user_id='spoofed-user') for _ in range(20)))
        assert Counter(r.status_code for r in replies) == {201: 1, 200: 19}
        assert len({r.json()['reservation_id'] for r in replies}) == 1
        reservation = replies[0].json()
        assert reservation['user_id'] != 'spoofed-user'
        assert reservation['amount_paise'] == 25000
        assert (await reserve(sid,['B'],alice,key)).status_code == 409
        assert (await reserve(sid,['A','B'],bob)).status_code == 409
        assert (await state(sid))['counts']['available'] == 2
        rid = reservation['reservation_id']
        assert (await client.post(f'/reservations/{rid}/cancel',headers=bob)).status_code == 403
        assert (await client.post(f'/reservations/{rid}/cancel',headers=alice)).status_code == 200
        assert (await reserve(sid,['A'],bob)).status_code == 201
        assert (await client.post(f'/reservations/{rid}/cancel',headers=alice)).status_code == 200
        assert (await reserve(sid,['A'],alice,key)).json()['status'] == 'cancelled'
        assert (await state(sid))['counts']['confirmed'] == 1

        # Reversed seat order tests deterministic locking and atomic bundles.
        sid = await show(['A','B'])
        replies = await asyncio.gather(reserve(sid,['A','B'],alice),reserve(sid,['B','A'],bob))
        assert Counter(r.status_code for r in replies) == {201: 1, 409: 1}
        assert (await state(sid))['counts']['confirmed'] == 2

        # Token setup is outside measured burst. Every contender has a unique identity.
        print(f'Preparing {requests} distinct users...', flush=True)
        run_id = str(uuid4())
        users = [None] * requests
        setup_jobs = iter(range(requests))
        worker_limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        async def prepare_worker():
            async with httpx.AsyncClient(base_url=base_url.rstrip('/'), timeout=180, limits=worker_limits, verify=tls) as transport:
                for i in setup_jobs:
                    users[i] = await token(f'burst-{run_id}-{i}', transport)
        await asyncio.gather(*(prepare_worker() for _ in range(min(20, requests))))
        print('User setup complete; starting measured hot-seat burst...', flush=True)
        sid = await show(['HOT','UNTOUCHED'])
        outcomes = Counter()
        latencies = []
        running = True
        snapshots = 0
        observation_errors = Counter()

        async def observe():
            nonlocal snapshots
            while running:
                try:
                    await state(sid)
                    snapshots += 1
                except (httpx.HTTPError, AssertionError) as exc:
                    observation_errors[type(exc).__name__] += 1
                await asyncio.sleep(0.1)

        async def attempt(headers, transport):
            start = time.monotonic()
            try:
                r = await reserve(sid,['HOT'],headers,transport=transport)
                if r.status_code == 201:
                    outcomes['confirmed'] += 1
                elif r.status_code >= 500:
                    outcomes['5xx'] += 1
                elif r.status_code == 409:
                    outcomes[r.json().get('error','unexpected_409')] += 1
                else:
                    outcomes[f'unexpected_{r.status_code}'] += 1
            except httpx.HTTPError:
                outcomes['transport_error'] += 1
            latencies.append(time.monotonic()-start)

        jobs = iter(users)
        async def burst_worker():
            async with httpx.AsyncClient(base_url=base_url.rstrip('/'), timeout=180, limits=worker_limits, verify=tls) as transport:
                for headers in jobs:
                    await attempt(headers, transport)

        monitor = asyncio.create_task(observe())
        started = time.monotonic()
        try:
            await asyncio.gather(*(burst_worker() for _ in range(min(concurrency, requests))))
        finally:
            running = False
            await monitor
        final = await state(sid)
        metric_response = await client.get('/metrics')
        metric_response.raise_for_status()
        assert f'seats_available{{show_id="{sid}"}} 1' in metric_response.text
        assert f'seats_confirmed{{show_id="{sid}"}} 1' in metric_response.text
        latencies.sort()
        report = {'base_url': base_url, 'requests': requests, 'concurrency': concurrency,
            'seconds': round(time.monotonic()-started,2),
            'outcomes': {'confirmed': 0, 'seat_taken': 0, '5xx': 0, 'transport_error': 0, **dict(outcomes)},
            'latency_p50_ms': round(latencies[len(latencies)//2]*1000,2),
            'latency_p99_ms': round(latencies[min(len(latencies)-1,int(len(latencies)*.99))]*1000,2),
            'snapshots_checked': snapshots, 'observation_errors': dict(observation_errors),
            'show_id': sid, 'reconciliation': final['counts']}
        print(json.dumps(report, indent=2))
        assert not observation_errors and snapshots > 0, report
        assert outcomes == {'confirmed': 1, 'seat_taken': requests-1}, report
        assert final['counts'] == {'available': 1, 'confirmed': 1, 'held': 0, 'total_seats': 2}
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('base_url')
    parser.add_argument('--requests', type=int, default=20000)
    parser.add_argument('--concurrency', type=int, default=500)
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.requests < 2 or args.concurrency < 1:
        parser.error('requests must be >= 2 and concurrency >= 1')
    secret = os.getenv('ADMIN_TOKEN')
    if not secret:
        sys.exit('Set ADMIN_TOKEN to the deployment admin token')
    report = asyncio.run(run(args.base_url, secret, args.requests, args.concurrency))
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2))
