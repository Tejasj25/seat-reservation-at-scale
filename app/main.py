import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Annotated
from uuid import UUID, uuid4

import jwt
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, StrictInt, field_validator

logger = logging.getLogger('seat_api')
logging.basicConfig(level=logging.INFO, format='%(message)s')
pool = None


@asynccontextmanager
async def lifespan(app):
    global pool
    for name in ('DATABASE_URL', 'ADMIN_TOKEN', 'JWT_SECRET'):
        if not os.environ.get(name):
            raise RuntimeError(f'{name} is required')
    if len(os.environ['JWT_SECRET']) < 32 or len(os.environ['ADMIN_TOKEN']) < 32:
        raise RuntimeError('ADMIN_TOKEN and JWT_SECRET must be at least 32 characters')
    pool = AsyncConnectionPool(os.environ['DATABASE_URL'], min_size=2,
        max_size=int(os.getenv('DB_POOL_SIZE', '20')), timeout=120,
        kwargs={'row_factory': dict_row, 'connect_timeout': 5}, open=False)
    await pool.open()
    await pool.wait(timeout=60)
    async with pool.connection() as conn:
        await conn.execute('SELECT pg_advisory_xact_lock(73281001)')
        await conn.execute(Path(__file__).with_name('schema.sql').read_text())
    yield
    await pool.close()


app = FastAPI(title='Seat Reservation API', version='1.0.0', lifespan=lifespan)


@app.middleware('http')
async def request_logging(request: Request, call_next):
    incoming = request.headers.get('x-request-id', '')
    rid = incoming if re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', incoming) else str(uuid4())
    start = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers['X-Request-ID'] = rid
        return response
    finally:
        route = request.scope.get('route')
        logger.info(json.dumps({'event': 'http_request', 'request_id': rid,
            'method': request.method, 'route': getattr(route, 'path', 'unmatched'),
            'status': status, 'duration_ms': round((time.monotonic()-start)*1000, 2)}))


async def db_unavailable(request, exc):
    logger.error(json.dumps({'event': 'database_unavailable', 'type': type(exc).__name__}))
    return JSONResponse({'error': 'dependency_unavailable'}, status_code=503,
                        headers={'Retry-After': '2'})


app.add_exception_handler(psycopg.OperationalError, db_unavailable)
app.add_exception_handler(PoolTimeout, db_unavailable)


def bearer(authorization):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Bearer token required', headers={'WWW-Authenticate': 'Bearer'})
    return authorization[7:]


def admin(authorization: Annotated[str | None, Header()] = None):
    if not hmac.compare_digest(bearer(authorization).encode(), os.environ['ADMIN_TOKEN'].encode()):
        raise HTTPException(403, 'Admin token required')


def user(authorization: Annotated[str | None, Header()] = None):
    try:
        claims = jwt.decode(bearer(authorization), os.environ['JWT_SECRET'],
            algorithms=['HS256'], issuer='seat-api', audience='seat-api',
            options={'require': ['sub', 'exp', 'iat', 'iss', 'aud']})
        if not isinstance(claims['sub'], str) or not 1 <= len(claims['sub']) <= 128:
            raise jwt.InvalidTokenError()
        return claims['sub']
    except jwt.InvalidTokenError:
        raise HTTPException(401, 'Invalid or expired token')


class TokenInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)


class SeatInput(BaseModel):
    seats: list[str] = Field(min_length=1, max_length=10000)

    @field_validator('seats')
    @classmethod
    def labels(cls, labels):
        if len(set(labels)) != len(labels) or any(not x.strip() or len(x) > 64 for x in labels):
            raise ValueError('Seat labels must be unique, nonempty, and at most 64 characters')
        return sorted(labels)


class ShowInput(SeatInput):
    name: str = Field(min_length=1, max_length=200)
    price_paise: StrictInt = Field(ge=0, le=1000000000000)
    per_user_limit: StrictInt = Field(default=4, ge=1, le=10000)


class ReserveInput(SeatInput):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


@app.post('/auth/tokens', dependencies=[Depends(admin)])
async def issue_token(body: TokenInput):
    now = int(time.time())
    token = jwt.encode({'sub': body.user_id, 'iat': now, 'exp': now+86400,
                        'iss': 'seat-api', 'aud': 'seat-api'}, os.environ['JWT_SECRET'], algorithm='HS256')
    return {'access_token': token, 'token_type': 'bearer', 'expires_in': 86400}


@app.get('/health/live')
async def live():
    return {'status': 'alive'}


@app.get('/health/ready')
async def ready():
    try:
        async with asyncio.timeout(3):
            async with pool.connection(timeout=2) as conn:
                await conn.execute('SELECT 1 FROM shows LIMIT 1')
        return {'status': 'ready'}
    except (psycopg.Error, PoolTimeout, TimeoutError):
        return JSONResponse({'status': 'not_ready'}, status_code=503)


async def show_state(conn, show_id):
    # One statement = one MVCC snapshot, including seat status and counts.
    row = await (await conn.execute('''SELECT sh.*,
        jsonb_agg(jsonb_build_object('seat', s.label, 'status',
          CASE WHEN s.reservation_id IS NULL THEN 'available' ELSE 'confirmed' END) ORDER BY s.label) AS seats,
        count(*) AS total_seats, count(*) FILTER(WHERE s.reservation_id IS NULL) AS available,
        count(*) FILTER(WHERE s.reservation_id IS NOT NULL) AS confirmed
        FROM shows sh JOIN seats s ON s.show_id=sh.id WHERE sh.id=%s GROUP BY sh.id''', (show_id,))).fetchone()
    if not row:
        raise HTTPException(404, 'Show not found')
    row['id'] = str(row['id'])
    row['counts'] = {k: row.pop(k) for k in ('total_seats', 'available', 'confirmed')}
    row['counts']['held'] = 0
    return row


@app.post('/shows', status_code=201, dependencies=[Depends(admin)])
async def create_show(body: ShowInput):
    sid = uuid4()
    async with pool.connection() as conn:
        await conn.execute('INSERT INTO shows VALUES(%s,%s,%s,%s)',
                           (sid, body.name, body.price_paise, body.per_user_limit))
        await conn.execute('INSERT INTO seats(show_id,label) SELECT %s, unnest(%s::text[])', (sid, body.seats))
        return await show_state(conn, sid)


@app.get('/shows/{show_id}')
async def get_show(show_id: UUID):
    async with pool.connection() as conn:
        return await show_state(conn, show_id)


def reservation_json(row):
    return {'reservation_id': str(row['id']), 'show_id': str(row['show_id']),
            'user_id': row['user_id'], 'seats': row['seats'],
            'amount_paise': row['amount_paise'], 'status': row['status']}


async def lock(conn, *parts):
    # Separate lock namespaces preserve ordering even if payload hashes collide.
    namespace = {'idempotency': 1, 'user_show': 2}[parts[0]]
    await conn.execute('SELECT pg_advisory_xact_lock(%s,hashtext(%s))',
                       (namespace, json.dumps(parts[1:], separators=(',', ':'))))


async def outcome(conn, reason, status=409, data=None):
    await conn.execute('INSERT INTO outcomes(reason) VALUES(%s)', (reason,))
    return JSONResponse(data or {'error': reason}, status_code=status,
        headers={'Idempotent-Replayed': 'true'} if reason == 'idempotent_replay' else {})


@app.post('/shows/{show_id}/reserve')
async def reserve(show_id: UUID, body: ReserveInput, uid=Depends(user),
                  idempotency_key: Annotated[str | None, Header(max_length=128)] = None):
    key = idempotency_key or body.idempotency_key
    if not key or (idempotency_key and body.idempotency_key and idempotency_key != body.idempotency_key):
        raise HTTPException(422, 'One consistent nonempty idempotency key is required')
    async with pool.connection() as conn:
        await lock(conn, 'idempotency', uid, key)
        old = await (await conn.execute('SELECT * FROM reservations WHERE user_id=%s AND idempotency_key=%s', (uid,key))).fetchone()
        if old:
            if old['show_id'] != show_id or old['seats'] != body.seats:
                return await outcome(conn, 'idempotency_conflict')
            return await outcome(conn, 'idempotent_replay', 200, reservation_json(old))
        fingerprint = await (await conn.execute('SELECT show_id,seats FROM reservation_keys WHERE user_id=%s AND idempotency_key=%s', (uid,key))).fetchone()
        if fingerprint and (fingerprint['show_id'] != show_id or fingerprint['seats'] != body.seats):
            return await outcome(conn, 'idempotency_conflict')
        show = await (await conn.execute('SELECT * FROM shows WHERE id=%s', (show_id,))).fetchone()
        if not show:
            raise HTTPException(404, 'Show not found')
        if not fingerprint:
            await conn.execute('INSERT INTO reservation_keys VALUES(%s,%s,%s,%s)', (uid,key,show_id,body.seats))
        await lock(conn, 'user_show', uid, str(show_id))
        count = await (await conn.execute("SELECT coalesce(sum(cardinality(seats)),0) AS n FROM reservations WHERE user_id=%s AND show_id=%s AND status='confirmed'", (uid,show_id))).fetchone()
        if count['n'] + len(body.seats) > show['per_user_limit']:
            return await outcome(conn, 'per_user_limit')
        seats = await (await conn.execute('SELECT * FROM seats WHERE show_id=%s AND label=ANY(%s) ORDER BY label FOR UPDATE', (show_id,body.seats))).fetchall()
        if len(seats) != len(body.seats):
            return await outcome(conn, 'unknown_seat', 404)
        if any(s['reservation_id'] for s in seats):
            return await outcome(conn, 'seat_taken')
        rid = uuid4()
        row = await (await conn.execute("INSERT INTO reservations(id,show_id,user_id,seats,amount_paise,status,idempotency_key) VALUES(%s,%s,%s,%s,%s,'confirmed',%s) RETURNING *",
            (rid,show_id,uid,body.seats,len(body.seats)*show['price_paise'],key))).fetchone()
        await conn.execute('UPDATE seats SET reservation_id=%s WHERE show_id=%s AND label=ANY(%s)', (rid,show_id,body.seats))
        return JSONResponse(reservation_json(row), status_code=201)


@app.post('/reservations/{reservation_id}/cancel')
async def cancel(reservation_id: UUID, uid=Depends(user)):
    async with pool.connection() as conn:
        row = await (await conn.execute('SELECT * FROM reservations WHERE id=%s', (reservation_id,))).fetchone()
        if not row:
            raise HTTPException(404, 'Reservation not found')
        if row['user_id'] != uid:
            raise HTTPException(403, 'Only the owner may cancel')
        await lock(conn, 'user_show', uid, str(row['show_id']))
        row = await (await conn.execute('SELECT * FROM reservations WHERE id=%s FOR UPDATE', (reservation_id,))).fetchone()
        if row['status'] != 'cancelled':
            await conn.execute('SELECT label FROM seats WHERE show_id=%s AND label=ANY(%s) ORDER BY label FOR UPDATE', (row['show_id'],row['seats']))
            await conn.execute('UPDATE seats SET reservation_id=NULL WHERE show_id=%s AND reservation_id=%s', (row['show_id'],reservation_id))
            await conn.execute("UPDATE reservations SET status='cancelled' WHERE id=%s", (reservation_id,))
            row['status'] = 'cancelled'
        return reservation_json(row)


@app.get('/metrics', response_class=PlainTextResponse)
async def metrics():
    async with pool.connection() as conn:
        await conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        totals = await (await conn.execute("SELECT count(*) AS confirmed, count(*) FILTER(WHERE status='cancelled') AS cancelled FROM reservations")).fetchone()
        reasons = await (await conn.execute('SELECT reason,count(*) AS n FROM outcomes GROUP BY reason')).fetchall()
        shows = await (await conn.execute('SELECT show_id,count(*) FILTER(WHERE reservation_id IS NULL) AS available,count(*) FILTER(WHERE reservation_id IS NOT NULL) AS confirmed,count(*) AS total FROM seats GROUP BY show_id')).fetchall()
    lines = ['# TYPE reservations_confirmed_total counter', f"reservations_confirmed_total {totals['confirmed']}",
             '# TYPE reservations_cancelled_total counter', f"reservations_cancelled_total {totals['cancelled']}",
             '# TYPE reservations_declined_total counter']
    counts = {r['reason']: r['n'] for r in reasons}
    for reason in ('seat_taken','per_user_limit','idempotent_replay','idempotency_conflict','unknown_seat'):
        lines.append(f'reservations_declined_total{{reason="{reason}"}} {counts.get(reason,0)}')
    for state in ('available','confirmed','held','total'):
        lines.append(f'# TYPE seats_{state} gauge')
        for sh in shows:
            lines.append(f'seats_{state}{{show_id="{sh["show_id"]}"}} {0 if state == "held" else sh[state]}')
    return PlainTextResponse('\n'.join(lines)+'\n', media_type='text/plain; version=0.0.4')
