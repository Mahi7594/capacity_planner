from django.contrib import admin
from .models import Employee, ProjectType, Segment, Category, Holiday, Project, Activity, GeneralSettings, CapacitySettings, EffortBracket, SalesForecast

admin.site.register(Employee)
admin.site.register(ProjectType)
admin.site.register(Segment)
admin.site.register(Category)
admin.site.register(Holiday)
admin.site.register(Project)
admin.site.register(Activity)
admin.site.register(GeneralSettings) 
admin.site.register(CapacitySettings) 
admin.site.register(EffortBracket)
admin.site.register(SalesForecast)