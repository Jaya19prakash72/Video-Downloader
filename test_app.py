import json
import app

def test_health():
    client = app.app.test_client()
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json['status'] == 'ok'

def test_rejects_unknown_url():
    client = app.app.test_client()
    response = client.post('/api/download', json={'url':'https://example.com/video','type':'video','quality':'720'})
    assert response.status_code == 400

def test_index_has_no_markdown_fence():
    client = app.app.test_client()
    response = client.get('/')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert not body.lstrip().startswith('```')
