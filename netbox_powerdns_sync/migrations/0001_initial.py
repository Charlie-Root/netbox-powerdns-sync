# Generated by Django 4.1.9 on 2023-07-24 10:08

import django.core.validators
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("dcim", "0172_larger_power_draw_values"),
        ("extras", "0092_delete_jobresult"),
    ]

    operations = [
        migrations.CreateModel(
            name="ApiServer",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=utilities.json.CustomFieldJSONEncoder,
                    ),
                ),
                ("name", models.CharField(max_length=100, unique=True)),
                ("description", models.CharField(blank=True, max_length=200)),
                ("enabled", models.BooleanField(default=True)),
                ("api_url", models.URLField(unique=True)),
                ("api_token", models.CharField(max_length=200)),
                (
                    "tags",
                    taggit.managers.TaggableManager(
                        through="extras.TaggedItem", to="extras.Tag"
                    ),
                ),
            ],
            options={
                "verbose_name": "API Server",
                "ordering": ("name",),
            },
        ),
        migrations.CreateModel(
            name="Zone",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        encoder=utilities.json.CustomFieldJSONEncoder,
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=200,
                        unique=True,
                        validators=[
                            django.core.validators.MinLengthValidator(3),
                            django.core.validators.RegexValidator(
                                "^(?:[a-zA-Z0-9][a-zA-Z0-9\\-]{0,63}\\.)*$",
                                "Only alphanumeric chars, hyphens and dots allowed",
                            ),
                            django.core.validators.RegexValidator(
                                "\\.$", "Zone name must end with a dot"
                            ),
                        ],
                    ),
                ),
                ("description", models.CharField(blank=True, max_length=200)),
                ("enabled", models.BooleanField(default=True)),
                ("is_default", models.BooleanField(default=False)),
                ("default_ttl", models.IntegerField(default=3600)),
                ("match_interface_mgmt_only", models.BooleanField(default=False)),
                (
                    "naming_ip_method",
                    models.CharField(default=None, max_length=200, null=True),
                ),
                (
                    "naming_device_method",
                    models.CharField(default=None, max_length=200, null=True),
                ),
                (
                    "naming_fgrpgroup_method",
                    models.CharField(default=None, max_length=200, null=True),
                ),
                (
                    "api_servers",
                    models.ManyToManyField(
                        related_name="zones", to="netbox_powerdns_sync.apiserver"
                    ),
                ),
                (
                    "match_device_roles",
                    models.ManyToManyField(
                        blank=True, related_name="+", to="dcim.devicerole"
                    ),
                ),
                (
                    "match_device_tags",
                    models.ManyToManyField(
                        blank=True, related_name="+", to="extras.tag"
                    ),
                ),
                (
                    "match_fhrpgroup_tags",
                    models.ManyToManyField(
                        blank=True, related_name="+", to="extras.tag"
                    ),
                ),
                (
                    "match_interface_tags",
                    models.ManyToManyField(
                        blank=True, related_name="+", to="extras.tag"
                    ),
                ),
                (
                    "match_ipaddress_tags",
                    models.ManyToManyField(
                        blank=True, related_name="+", to="extras.tag"
                    ),
                ),
                (
                    "tags",
                    taggit.managers.TaggableManager(
                        through="extras.TaggedItem", to="extras.Tag"
                    ),
                ),
            ],
            options={
                "ordering": ("name",),
            },
        ),
        migrations.AddConstraint(
            model_name="zone",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_default", True)),
                fields=("is_default",),
                name="unique_is_default",
                violation_error_message="Only one zone can be set as default",
            ),
        ),
    ]
