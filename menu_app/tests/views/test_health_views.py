from django.urls import reverse


def test_health_endpoint_is_available_without_login_or_database(client):
    assert client.get(reverse('health')).status_code == 200
