from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apis', '0002_analysisjob_report_alert_snapshot_url_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                choices=[('ADMIN', 'Administrator'), ('INSTRUCTOR', 'Instructor')],
                default='INSTRUCTOR',
                max_length=20,
            ),
        ),
    ]
