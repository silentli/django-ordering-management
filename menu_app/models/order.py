from decimal import Decimal
from typing import ClassVar, List, Tuple

from django.db import models

from menu_app.models.menu_item import MAX_ITEM_PRICE, MenuCategory, MenuItem


class OrderStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    COMPLETED = 'completed', 'Completed'
    CANCELLED = 'cancelled', 'Cancelled'


class Order(models.Model):
    """
    Represents a customer order with its items and status.
    """

    STATUS_CHOICES: ClassVar[List[Tuple[str, str]]] = OrderStatus.choices

    # Fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    class Meta:
        ordering: ClassVar[List[str]] = ['-created_at']
        constraints: ClassVar[List[models.CheckConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(status__in=OrderStatus.values), name='order_status_valid'
            ),
        ]

    def __str__(self):
        return f'Order {self.id} ({self.status})'

    @property
    def total_items(self) -> int:
        """Total quantity across all order lines."""
        return sum(item.quantity for item in self.items.all())

    @property
    def total_price(self) -> Decimal:
        """Calculate the total price of the order."""
        return sum((item.subtotal for item in self.items.all()), Decimal('0.00'))


class OrderItem(models.Model):
    """
    Represents an item within an order.
    """

    # Fields
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    menu_item = models.ForeignKey(MenuItem, on_delete=models.PROTECT)
    name_at_time_of_order = models.CharField(max_length=100)
    category_at_time_of_order = models.CharField(max_length=100, choices=MenuCategory.choices)
    quantity = models.PositiveIntegerField(default=1)
    price_at_time_of_order = models.DecimalField(max_digits=6, decimal_places=2)

    class Meta:
        verbose_name = 'Order Item'
        verbose_name_plural = 'Order Items'
        ordering: ClassVar[List[str]] = ['order', 'name_at_time_of_order']
        unique_together: ClassVar[List[str]] = ['order', 'menu_item']
        constraints: ClassVar[List[models.CheckConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name='order_item_quantity_positive'
            ),
            models.CheckConstraint(
                condition=models.Q(
                    price_at_time_of_order__gt=0,
                    price_at_time_of_order__lte=MAX_ITEM_PRICE,
                ),
                name='order_item_price_positive',
            ),
        ]

    def __str__(self):
        return f'{self.quantity} x {self.name_at_time_of_order} (${self.subtotal})'

    @property
    def subtotal(self) -> Decimal:
        """Calculate the subtotal for this item."""
        return self.quantity * self.price_at_time_of_order
