from django.test import TestCase
from django.urls import reverse

from rest_framework import status
from rest_framework.test import APITestCase

from django.contrib.auth.models import User
# Create your tests here.


class APITests(APITestCase):
    
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create(
            id=99,
            username='testuser',
            email='testuser@example.com',
        )
        
        
    def test_api_listview(self):
        response = self.client.get(reverse('users_list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(User.objects.count(), 1)
        self.assertContains(response, self.user)
        
        