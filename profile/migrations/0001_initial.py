import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('tsp', '0006_groups'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='UserProfile',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('changed_at', models.DateTimeField(auto_now=True)),
                        ('phone', models.CharField(blank=True, max_length=50)),
                        ('address_line', models.CharField(blank=True, max_length=200)),
                        ('city', models.CharField(blank=True, max_length=100)),
                        ('state', models.CharField(blank=True, max_length=100)),
                        ('country', models.CharField(blank=True, max_length=100)),
                        ('postal_code', models.CharField(blank=True, max_length=20)),
                        ('profile_completed_at', models.DateTimeField(blank=True, null=True)),
                        ('banking_method', models.CharField(
                            blank=True,
                            choices=[
                                ('iban', 'International (IBAN)'),
                                ('us_bank', 'United States'),
                                ('india_ifsc', 'India (IFSC)'),
                            ],
                            max_length=20,
                            null=True,
                        )),
                        ('banking_currency', models.CharField(default='EUR', max_length=3)),
                        ('wise_account_id', models.PositiveIntegerField(blank=True, null=True)),
                        ('wise_recipient_verified_at', models.DateTimeField(blank=True, null=True)),
                        ('changed_by', models.ForeignKey(
                            blank=True,
                            null=True,
                            on_delete=django.db.models.deletion.SET_NULL,
                            related_name='%(app_label)s_%(class)s_changed',
                            to=settings.AUTH_USER_MODEL,
                        )),
                        ('created_by', models.ForeignKey(
                            blank=True,
                            null=True,
                            on_delete=django.db.models.deletion.SET_NULL,
                            related_name='%(app_label)s_%(class)s_created',
                            to=settings.AUTH_USER_MODEL,
                        )),
                        ('user', models.OneToOneField(
                            on_delete=django.db.models.deletion.CASCADE,
                            related_name='profile',
                            to=settings.AUTH_USER_MODEL,
                        )),
                    ],
                    options={
                        'db_table': 'tsp_userprofile',
                        'ordering': ['user__username'],
                        'permissions': [
                            ('submit_travel_requests', 'Can submit travel requests'),
                            ('review_travel_requests', 'Can review and approve travel requests'),
                            ('manage_events', 'Can manage TSP events'),
                            ('view_reports', 'Can view TSP reports'),
                            ('process_payments', 'Can process reimbursements'),
                        ],
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
