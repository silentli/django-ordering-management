import pytest
from django.contrib.messages import get_messages
from django.db import IntegrityError
from django.urls import reverse

from menu_app.models import InventoryItem, MenuItem, Order, OrderItem


@pytest.mark.django_db
def test_cart_management(client, menu_item):
    """Test cart management functionality."""
    # Test adding item to cart
    response = client.post(
        reverse('menu_app:menu_list'), {'menu_item_id': menu_item.id, 'action': 'add'}
    )
    assert response.status_code == 302

    # Check cart in session
    session = client.session
    assert 'cart' in session
    assert session['cart'][str(menu_item.id)] == 1

    # Test updating item quantity
    response = client.post(
        reverse('menu_app:menu_list'),
        {'menu_item_id': menu_item.id, 'action': 'update', 'quantity': 2},
    )
    assert response.status_code == 302
    session = client.session
    assert session['cart'][str(menu_item.id)] == 2

    # Test removing item from cart
    response = client.post(
        reverse('menu_app:menu_list'),
        {'menu_item_id': menu_item.id, 'action': 'remove'},
    )
    assert response.status_code == 302

    # Check cart is empty
    session = client.session
    assert str(menu_item.id) not in session.get('cart', {})


@pytest.mark.django_db
def test_checkout_process(client, menu_item, inventory_item, test_data):
    """Test checkout process."""
    # Add item to cart and checkout
    client.post(reverse('menu_app:menu_list'), {'menu_item_id': menu_item.id, 'action': 'add'})
    response = client.post(reverse('menu_app:menu_list'), {'action': 'checkout'})
    assert response.status_code == 302  # Redirect after checkout

    # Check cart is empty after checkout
    session = client.session
    session.save()  # Save the session to persist changes
    assert 'cart' not in session

    # Verify inventory was updated
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == test_data['quantity'] - 1  # One item was ordered

    placed_order = Order.objects.get()
    item = placed_order.items.get()
    assert item.menu_item == menu_item
    assert item.quantity == 1
    assert item.price_at_time_of_order == menu_item.price


@pytest.mark.django_db
def test_checkout_failure_rolls_back_and_preserves_cart(
    client, menu_item, inventory_item, test_data
):
    """A later item failure must undo earlier items and preserve the selection for retry."""
    unavailable = MenuItem.objects.create(name='Unavailable', category='beverage', price='2.50')
    unavailable_stock = InventoryItem.objects.create(menu_item=unavailable, quantity=0)
    failed_id = unavailable.id

    cart = {str(menu_item.id): 2, str(failed_id): 1}
    session = client.session
    session['cart'] = cart
    session.save()

    response = client.post(reverse('menu_app:menu_list'), {'action': 'checkout'})

    assert response.status_code == 302
    assert client.session['cart'] == cart
    assert not Order.objects.exists()
    assert not OrderItem.objects.exists()
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == test_data['quantity']
    notifications = list(get_messages(response.wsgi_request))
    assert any(message.level_tag == 'error' for message in notifications)
    assert not any(message.level_tag == 'success' for message in notifications)

    unavailable_stock.quantity = 1
    unavailable_stock.save()
    response = client.post(reverse('menu_app:menu_list'), {'action': 'checkout'})
    assert response.status_code == 302
    assert 'cart' not in client.session
    assert Order.objects.count() == 1
    assert OrderItem.objects.count() == 2
    inventory_item.refresh_from_db()
    unavailable_stock.refresh_from_db()
    assert inventory_item.quantity == test_data['quantity'] - 2
    assert unavailable_stock.quantity == 0


@pytest.mark.django_db
def test_empty_checkout_does_not_create_order(client):
    response = client.post(reverse('menu_app:menu_list'), {'action': 'checkout'})
    assert response.status_code == 302
    assert not Order.objects.exists()


@pytest.mark.django_db
def test_unexpected_checkout_failure_rolls_back_and_keeps_diagnostics_private(
    client, menu_item, inventory_item, test_data, monkeypatch
):
    cart = {str(menu_item.pk): 2}
    session = client.session
    session['cart'] = cart
    session.save()
    create_line = OrderItem.objects.create
    private_diagnostic = 'private storage diagnostic'

    def failing_create(*args, **kwargs):
        create_line(*args, **kwargs)
        raise IntegrityError(private_diagnostic)

    monkeypatch.setattr(OrderItem.objects, 'create', failing_create)
    response = client.post(reverse('menu_app:menu_list'), {'action': 'checkout'}, follow=True)

    assert response.status_code == 200
    assert private_diagnostic not in response.content.decode()
    assert any(message.level_tag == 'error' for message in get_messages(response.wsgi_request))
    assert client.session['cart'] == cart
    assert not Order.objects.exists()
    assert not OrderItem.objects.exists()
    inventory_item.refresh_from_db()
    assert inventory_item.quantity == test_data['quantity']
