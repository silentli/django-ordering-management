import pytest
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.urls import reverse

from menu_app.models import InventoryItem, MenuItem, Order
from menu_app.services import inventory_service, menu_service, order_service


@pytest.fixture
def staff_user():
    """Fixture providing a staff user."""
    return User.objects.create_user(username='staff', is_staff=True)


@pytest.mark.django_db
@pytest.mark.parametrize('existing_user', [False, True], ids=['first-login', 'changed-password'])
def test_staff_login_creates_or_updates_account(client, settings, existing_user):
    settings.STAFF_PASSWORD = 'new-staff-password'
    if existing_user:
        original = User.objects.create_user(
            username='staff', password='old-staff-password', is_staff=True
        )

    response = client.post(reverse('menu_app:staff_login'), {'password': settings.STAFF_PASSWORD})

    staff_user = User.objects.get(username='staff')
    assert response.status_code == 302
    assert client.session.get('_auth_user_id') == str(staff_user.id)
    assert staff_user.is_staff and staff_user.is_active
    assert staff_user.check_password(settings.STAFF_PASSWORD)
    assert User.objects.count() == 1
    if existing_user:
        assert staff_user.pk == original.pk
        assert not staff_user.check_password('old-staff-password')


@pytest.mark.django_db
def test_staff_login_failure(client):
    """Test failed staff login."""
    response = client.post(reverse('menu_app:staff_login'), {'password': 'wrong_password'})
    assert response.status_code == 200  # Stay on login page
    assert '_auth_user_id' not in client.session
    assert not User.objects.filter(username='staff').exists()


@pytest.mark.django_db
def test_staff_login_rejects_missing_password_configuration(client, settings):
    settings.STAFF_PASSWORD = None

    response = client.post(reverse('menu_app:staff_login'), {})

    assert response.status_code == 200
    assert '_auth_user_id' not in client.session
    assert not User.objects.filter(username='staff').exists()


@pytest.mark.django_db
def test_order_management_view(client, staff_user, menu_item, inventory_item):
    """Test order management functionality."""
    client.force_login(staff_user)
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
    client.force_login(staff_user)

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
def test_inventory_update_rejects_stale_stock(client, staff_user, menu_item, inventory_item):
    client.force_login(staff_user)
    # A sale after the page was loaded changes the stock from 5 to 4.
    inventory_service.remove_stock(menu_item, 1)
    data = {'quantity': 50, 'expected_quantity': 5}

    response = client.post(reverse('menu_app:inventory_update', args=[menu_item.id]), data)

    assert response.status_code == 302
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 4
    assert any(message.level_tag == 'error' for message in get_messages(response.wsgi_request))


@pytest.mark.django_db
def test_menu_deactivation_preserves_orders_and_blocks_checkout(
    client, staff_user, menu_item, inventory_item
):
    original_name = menu_item.name
    original_category_label = menu_item.get_category_display()
    order = order_service.place_order({str(menu_item.pk): 2})
    client.force_login(staff_user)
    response = client.post(
        reverse('menu_app:staff_menu_update', args=[menu_item.pk]),
        {'name': 'Renamed dish', 'price': '20.00', 'category': 'dessert'},
    )
    assert response.status_code == 302
    menu_item.refresh_from_db()
    assert not menu_item.is_active
    assert menu_item not in client.get(reverse('menu_app:menu_list')).context['menus']
    assert menu_item in client.get(reverse('menu_app:staff_menu_list')).context['menus']

    detail = client.get(reverse('menu_app:staff_order_detail', args=[order.pk]))
    assert original_name in detail.content.decode()
    assert original_category_label in detail.content.decode()
    assert 'Renamed dish' not in detail.content.decode()

    session = client.session
    session['cart'] = {str(menu_item.pk): 1}
    session.save()
    client.post(reverse('menu_app:menu_list'), {'action': 'checkout'})
    assert client.session['cart'] == {str(menu_item.pk): 1}
    assert Order.objects.count() == 1
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 3

    response = client.post(reverse('menu_app:staff_menu_delete', args=[menu_item.pk]))
    assert response.status_code == 302
    assert MenuItem.objects.filter(pk=menu_item.pk).exists()
    order_service.cancel_order(order.pk, actor=staff_user)
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == 5

    menu_service.modify_menu_item(menu_item, is_active=True)
    new_order = order_service.place_order({str(menu_item.pk): 1})
    assert new_order.items.get().name_at_time_of_order == 'Renamed dish'


@pytest.mark.django_db
def test_customers_cannot_change_menu_availability(client, menu_item, django_user_model):
    customer = django_user_model.objects.create_user(username='menu-customer')
    client.force_login(customer)
    response = client.post(
        reverse('menu_app:staff_menu_update', args=[menu_item.pk]),
        {'name': menu_item.name, 'price': '10.00', 'category': 'main'},
    )
    assert response.status_code == 302
    menu_item.refresh_from_db()
    assert menu_item.is_active


@pytest.mark.django_db
def test_low_stock_page_uses_each_items_threshold(client, staff_user):
    client.force_login(staff_user)
    cases = [(2, 3), (5, 5), (7, 6), (12, 15), (0, 1)]
    stocks = []
    for index, (quantity, threshold) in enumerate(cases):
        item = MenuItem.objects.create(name=f'Dish {index}', category='main', price='1.00')
        stocks.append(
            InventoryItem.objects.create(
                menu_item=item, quantity=quantity, low_stock_threshold=threshold
            )
        )

    response = client.get(reverse('menu_app:low_stock_list'))

    assert response.status_code == 200
    assert {stock.pk for stock in response.context['low_stock_items']} == {
        stocks[0].pk,
        stocks[1].pk,
        stocks[3].pk,
        stocks[4].pk,
    }
