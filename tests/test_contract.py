import os
from uuid import uuid4

import httpx


def test_auth_validation_and_key_scope():
    with httpx.Client(base_url=os.getenv('BASE_URL', 'http://localhost:8000'), timeout=20) as client:
        admin = {'Authorization': 'Bearer '+os.environ['ADMIN_TOKEN']}
        assert client.post('/shows', json={}).status_code == 401
        assert client.post('/auth/tokens', json={'user_id': 'forged'},
                           headers={'Authorization': 'Bearer wrong'}).status_code == 403
        uid = 'contract-'+str(uuid4())
        token = client.post('/auth/tokens', headers=admin, json={'user_id': uid}).json()['access_token']
        user = {'Authorization': 'Bearer '+token}
        spec = {'name': 'contract', 'seats': ['A','B'], 'price_paise': 25000}
        assert client.post('/shows', headers=admin, json={**spec, 'price_paise': 1.5}).status_code == 422
        assert client.post('/shows', headers=admin, json={**spec, 'price_paise': True}).status_code == 422
        assert client.post('/shows', headers=admin, json={**spec, 'seats': ['A','A']}).status_code == 422
        show = client.post('/shows', headers=admin, json=spec).json()['id']
        path = f'/shows/{show}/reserve'
        assert client.post(path, headers=admin, json={'seats':['A'],'idempotency_key':'x'}).status_code == 401
        assert client.post(path, headers=user, json={'seats':['A']}).status_code == 422
        assert client.post(path, headers={**user,'Idempotency-Key':'header'},
                           json={'seats':['A'],'idempotency_key':'body'}).status_code == 422
        response = client.post(path, headers={**user,'Idempotency-Key':'header'}, json={'seats':['A']})
        assert response.status_code == 201
        assert response.json()['user_id'] == uid
        assert response.headers['X-Request-ID']
        replay = client.post(path, headers={**user,'Idempotency-Key':'header','X-Request-ID':'contract-test'},
                             json={'seats':['A']})
        assert replay.status_code == 200
        assert replay.headers['Idempotent-Replayed'] == 'true'
        assert replay.headers['X-Request-ID'] == 'contract-test'
        other = client.post('/shows', headers=admin, json=spec).json()['id']
        response = client.post(f'/shows/{other}/reserve',headers=user,
                               json={'seats':['A'],'idempotency_key':'header'})
        assert response.status_code == 409 and response.json()['error'] == 'idempotency_conflict'
        response = client.post(path, headers=user, json={'seats':['MISSING','B'],'idempotency_key':'unknown'})
        assert response.status_code == 404
        assert client.get(f'/shows/{show}').json()['counts']['available'] == 1
