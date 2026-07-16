"""Tax data URL configuration."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import FederalTaxYearViewSet, StateTaxProfileViewSet
from .views_pa import PaTaxCodeViewSet
from .views_calculate import calculate_view

router = DefaultRouter()
router.register(r'federal', FederalTaxYearViewSet, basename='federal-tax-year')
router.register(r'states', StateTaxProfileViewSet, basename='state-tax-profile')
router.register(r'pa/pa-codes', PaTaxCodeViewSet, basename='pa-tax-code')

urlpatterns = [
    path('auth/token/', TokenObtainPairView.as_view(), name='tax-admin-token'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='tax-admin-token-refresh'),
    path('calculate/', calculate_view, name='tax-calculate'),
    path('', include(router.urls)),
]
