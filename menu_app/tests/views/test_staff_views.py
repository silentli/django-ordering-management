import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.urls import reverse

from menu_app.models import MenuItem
from menu_app.services import inventory_service, order_service

# Test password for staff authentication
TEST_STAFF_PASSWORD = 'test_staff_password'  # Must match the value in conftest.py


@pytest.fixture
def staff_user():
    """Fixture providing a staff user."""
    user, _created = User.objects.get_or_create(
        username='staff', defaults={'is_staff': True, 'is_active': True}
    )
    # Set the password to match settings.STAFF_PASSWORD
    user.set_password(settings.STAFF_PASSWORD)
    user.save()
    return user


@pytest.fixture
def staff_data():
    """Fixture providing staff-specific test data."""
    return {'username': 'staff', 'password': TEST_STAFF_PASSWORD, 'is_staff': True}


@pytest.mark.django_db
def test_staff_login_success(client, staff_user):
    """Test successful staff login."""
    response = client.post(reverse('menu_app:staff_login'), {'password': settings.STAFF_PASSWORD})
    assert response.status_code == 302  # Redirect after successful login
    assert client.session.get('_auth_user_id') == str(staff_user.id)


@pytest.mark.django_db
def test_staff_login_failure(client):
    """Test failed staff login."""
    response = client.post(reverse('menu_app:staff_login'), {'password': 'wrong_password'})
    assert response.status_code == 200  # Stay on login page
    assert '_auth_user_id' not in client.session


@pytest.mark.django_db
def test_menu_form_rejects_zero_price(client, staff_user):
    client.force_login(staff_user)
    response = client.post(
        reverse('menu_app:staff_menu_create'),
        {'name': 'Invalid price', 'category': 'main', 'price': '0.00'},
    )
    assert response.status_code == 200
    assert 'price' in response.context['form'].errors
    assert not MenuItem.objects.exists()


@pytest.mark.django_db
def test_staff_dashboard_view(client, staff_user):
    """Test staff dashboard access."""
    # Test without login
    response = client.get(reverse('menu_app:staff_dashboard'))
    assert response.status_code == 302  # Redirect to login

    # Test with login
    client.login(username='staff', password=settings.STAFF_PASSWORD)
    response = client.get(reverse('menu_app:staff_dashboard'))
    assert response.status_code == 200
    assert 'orders' in response.context


@pytest.mark.django_db
def test_order_management_view(client, staff_user, menu_item, inventory_item):
    """Test order management functionality."""
    client.login(username='staff', password=settings.STAFF_PASSWORD)
    order = order_service.place_order({str(menu_item.pk): 1})

    # Test order list view
    response = client.get(reverse('menu_app:staff_order_list'))
    assert response.status_code == 200
    assert str(order.id) in response.content.decode()

    # Test completing an order
    response = client.post(
        reverse('menu_app:staff_order_detail', args=[order.id]), {'action': 'complete'}
    )
    assert response.status_code == 302  # Redirect after completion
    order.refresh_from_db()
    assert order.status == 'completed'
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 4

    # Test canceling an order
    new_order = order_service.place_order({str(menu_item.pk): 1})
    response = client.post(
        reverse('menu_app:staff_order_detail', args=[new_order.id]),
        {'action': 'cancel'},
    )
    assert response.status_code == 302  # Redirect after cancellation
    new_order.refresh_from_db()
    assert new_order.status == 'cancelled'
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 4


@pytest.mark.django_db
def test_customers_cannot_cancel_orders(client, django_user_model, menu_item, inventory_item):
    placed_order = order_service.place_order({str(menu_item.pk): 2})
    url = reverse('menu_app:staff_order_detail', args=[placed_order.pk])

    assert client.post(url, {'action': 'cancel'}).status_code in (302, 403)
    customer = django_user_model.objects.create_user(username='customer')
    client.force_login(customer)
    assert client.post(url, {'action': 'cancel'}).status_code in (302, 403)

    placed_order.refresh_from_db()
    inventory_item.refresh_from_db()
    assert placed_order.status == 'pending'
    assert inventory_item.quantity == 3


@pytest.mark.django_db
def test_inventory_management_view(client, staff_user, menu_item, inventory_item):
    """Test inventory management functionality."""
    client.login(username='staff', password=settings.STAFF_PASSWORD)

    # Test inventory list view
    response = client.get(reverse('menu_app:inventory_list'))
    assert response.status_code == 200
    assert str(menu_item.name) in response.content.decode()

    # Test updating inventory
    target_quantity = 50

    response = client.post(
        reverse('menu_app:inventory_update', args=[menu_item.id]),
        {'quantity': target_quantity, 'expected_quantity': inventory_item.quantity},
    )
    assert response.status_code == 302  # Redirect after update

    # Force a fresh database query to get the latest state
    updated_inventory = inventory_service.get_inventory(menu_item)
    assert updated_inventory is not None
    assert updated_inventory.quantity == target_quantity


@pytest.mark.django_db
@pytest.mark.parametrize('quantity,expected_quantity', [(50, 5), (50, None), (-1, 4)])
def test_inventory_update_rejects_stale_or_invalid_input(
    client, staff_user, menu_item, inventory_item, quantity, expected_quantity
):
    client.force_login(staff_user)
    # A sale after the page was loaded changes the stock from 5 to 4.
    inventory_service.remove_stock(menu_item, 1)
    data = {'quantity': quantity}
    if expected_quantity is not None:
        data['expected_quantity'] = expected_quantity

    response = client.post(reverse('menu_app:inventory_update', args=[menu_item.id]), data)

    assert response.status_code == 302
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 4
    assert any(message.level_tag == 'error' for message in get_messages(response.wsgi_request))
