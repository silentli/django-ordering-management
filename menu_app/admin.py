from django.contrib import admin

from menu_app.models.inventory import InventoryItem
from menu_app.models.menu_item import MenuItem
from menu_app.models.order import Order, OrderItem


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'price', 'is_active')
    list_filter = ('category', 'is_active')
    search_fields = ('name',)


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ('menu_item', 'quantity')
    search_fields = ('menu_item__name',)
    readonly_fields = ('quantity',)

    def save_model(self, request, obj, form, change):
        if change:
            # Stock is managed through the staff inventory page. Editing other
            # fields must not overwrite sales or restocking that happened meanwhile.
            obj.save(update_fields=form.changed_data)
        else:
            super().save_model(request, obj, form, change)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 1
    readonly_fields = (
        'name_at_time_of_order',
        'category_at_time_of_order',
        'price_at_time_of_order',
    )


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'status', 'created_at', 'total_price')
    list_filter = ('status', 'created_at')
    inlines = [OrderItemInline]
    readonly_fields = ('created_at', 'updated_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
