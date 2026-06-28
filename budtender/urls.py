from django.urls import path

from . import views

urlpatterns = [
    path("", views.begin, name="begin"),
    path("pos/", views.screen, name="screen"),
    path("start/", views.start, name="start"),
    path("end/", views.end_session, name="end"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("scan/", views.scan, name="scan"),
    path("lookup/", views.lookup, name="lookup"),
    path("profile/", views.profile, name="profile"),
    path("customer/", views.customer, name="customer"),
    path("customer/full/", views.customer_full, name="customer_full"),
    path("menu/", views.menu, name="menu"),
    path("product/<product_id>/", views.product, name="product"),
    path("cart/add/", views.cart_add, name="cart_add"),
    path("cart/remove/", views.cart_remove, name="cart_remove"),
    path("cart/submit/", views.cart_submit, name="cart_submit"),
]
