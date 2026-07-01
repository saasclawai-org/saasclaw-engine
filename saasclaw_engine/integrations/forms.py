from django import forms

from .models import GitHubInstallation


class ProjectGitHubInstallationForm(forms.Form):
    installation = forms.ModelChoiceField(
        queryset=GitHubInstallation.objects.none(),
        empty_label='Select a GitHub installation',
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = GitHubInstallation.objects.order_by('account_name', 'installation_id')
        if user and user.is_authenticated:
            qs = qs.filter(user=user)
        self.fields['installation'].queryset = qs


class ProjectConnectRepoForm(forms.Form):
    repo_name = forms.CharField(max_length=255, help_text='Repository name under the selected installation.')
    default_branch = forms.CharField(max_length=100, required=False, initial='main')
