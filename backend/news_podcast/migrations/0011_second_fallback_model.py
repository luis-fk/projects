import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('news_podcast', '0010_lesson_every_days'),
    ]

    operations = [
        migrations.AddField(
            model_name='podcastsettings',
            name='second_fallback_model',
            field=models.CharField(blank=True, default='', help_text='Writes the text when the text model and the fallback are unavailable. Optional.', max_length=100),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='podcastsettings',
            name='second_fallback_input_price',
            field=models.FloatField(default=0, validators=[django.core.validators.MinValueValidator(0)], verbose_name='input price'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='podcastsettings',
            name='second_fallback_output_price',
            field=models.FloatField(default=0, validators=[django.core.validators.MinValueValidator(0)], verbose_name='output price'),
            preserve_default=False,
        ),
    ]
