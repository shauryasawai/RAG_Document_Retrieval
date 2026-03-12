from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth.models import User
from .models import UserProfile

class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)
    company = forms.CharField(max_length=200, required=False)
    openai_api_key = forms.CharField(max_length=200, required=False, 
                                      widget=forms.PasswordInput(render_value=True),
                                      help_text="Your OpenAI API key (sk-...)")

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']

class LoginForm(AuthenticationForm):
    pass

class ProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['company', 'openai_api_key']
        widgets = {
            'openai_api_key': forms.PasswordInput(render_value=True),
        }
