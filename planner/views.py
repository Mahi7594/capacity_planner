# planner/views.py

from django.shortcuts import render, redirect, get_object_or_404
from .models import (Employee, ProjectType, Segment, Category, Holiday, 
                     Project, Activity, GeneralSettings, CapacitySettings, 
                     SalesForecast, EffortBracket)
from datetime import date, timedelta, datetime
from collections import OrderedDict, defaultdict
from django.db.models import Min, Max
from .forms import ActivityForm, ProjectForm
from django.urls import reverse
from urllib.parse import urlencode
from django.http import JsonResponse
import json
from .utils import calculate_end_date, count_working_days, calculate_effort_from_value
import calendar
from django.views.decorators.http import require_POST

# Define this constant at the top of the file to avoid "magic numbers"
CR = 10_000_000

# MODIFIED: Helper function is now much simpler.
def _prepare_gantt_context(activities_qs):
    """
    Takes a queryset of activities and returns a context dictionary 
    with the date range and header data for a Gantt chart.
    
    All work_day and overlap calculation is moved to the frontend.
    """
    activities_list = list(activities_qs)
    today = date.today()
    
    holidays_map = {h.date: h.description for h in Holiday.objects.all()}

    # 1. Determine the date range for the Gantt chart
    min_start_dates = [a.start_date for a in activities_list if a.start_date]
    max_end_dates = [a.end_date for a in activities_list if a.end_date]
    gantt_start_date = min(min_start_dates) - timedelta(days=7) if min_start_dates else today - timedelta(days=7)
    gantt_end_date = max(max_end_dates) + timedelta(days=60) if max_end_dates else today + timedelta(days=60)
            
    # 2. Build the gantt_data dictionary for headers
    gantt_data = {'start_date': gantt_start_date, 'end_date': gantt_end_date, 'months': OrderedDict()}
    header_dates = [gantt_start_date + timedelta(days=i) for i in range((gantt_end_date - gantt_start_date).days + 1)]
    for d in header_dates:
        month_year = d.strftime("%B %Y")
        gantt_data['months'][month_year] = gantt_data['months'].get(month_year, 0) + 1
    gantt_data['header_dates'] = header_dates
    
    return {
        'activities': activities_list, # Pass the original list for grouping
        'gantt_data': gantt_data,
        'today': today,
        'holidays_map': holidays_map,
    }

def sales_forecast_view(request):
    if request.method == 'POST':
        if 'save_data' in request.POST:
            data = json.loads(request.POST.get('data', '[]'))
            SalesForecast.objects.all().delete()  # Clear existing data first
            
            for item in data:
                opportunity_id = item.get('Opportunity', '')
                if not opportunity_id: 
                    continue
                    
                try:
                    # Handle the Total Amount - check both possible keys
                    amount_str = str(item.get('Total Amount (in Cr)', item.get('Total Amount', '0'))).replace(',', '')
                    total_amount = float(amount_str) * CR if amount_str else 0.0
                    
                    # Handle probability
                    prob_str = str(item.get('Probability(%)', '0')).replace('%', '')
                    probability = float(prob_str) if prob_str else 0.0
                    
                    # Handle dates - support both Y-m-d and d-m-Y formats
                    start_date_val = None
                    end_date_val = None
                    
                    start_date_str = item.get('Start Date', '')
                    if start_date_str:
                        try:
                            # Try Y-m-d format first
                            start_date_val = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                        except ValueError:
                            try:
                                # Try d-m-Y format
                                start_date_val = datetime.strptime(start_date_str, '%d-%m-%Y').date()
                            except ValueError:
                                print(f"Could not parse start date: {start_date_str}")
                    
                    end_date_str = item.get('End date', '')
                    if end_date_str:
                        try:
                            # Try Y-m-d format first
                            end_date_val = datetime.strptime(end_date_str, '%Y-%m-%d').date()
                        except ValueError:
                            try:
                                # Try d-m-Y format
                                end_date_val = datetime.strptime(end_date_str, '%d-%m-%Y').date()
                            except ValueError:
                                print(f"Could not parse end date: {end_date_str}")
                    
                    SalesForecast.objects.update_or_create(
                        opportunity=opportunity_id,
                        defaults={
                            'total_amount': total_amount,
                            'probability': probability,
                            'segment': item.get('Segment', ''),
                            'category': item.get('Category', ''),
                            'solution': item.get('Solution', ''),
                            'start_date': start_date_val,
                            'end_date': end_date_val
                        }
                    )
                except (ValueError, TypeError) as e:
                    print(f"Could not process row for {opportunity_id}: {e}")
                    continue
                    
            return JsonResponse({'status': 'success'})
            
        if 'delete_all' in request.POST:
            SalesForecast.objects.all().delete()
            return redirect('sales_forecast')

    # Calculate effort for display
    project_types_with_brackets = ProjectType.objects.prefetch_related('effort_brackets')
    pt_bracket_map = {pt.id: list(pt.effort_brackets.all()) for pt in project_types_with_brackets}
    pt_map = {(pt.segment.name, pt.category.name): pt.id for pt in ProjectType.objects.select_related('segment', 'category')}
    
    forecast_data = list(SalesForecast.objects.all())
    for item in forecast_data:
        pt_id = pt_map.get((item.segment, item.category))
        brackets = pt_bracket_map.get(pt_id, [])
        item.calculated_effort = calculate_effort_from_value(item.total_amount, brackets)
        # Divide by conversion factor for display in Cr
        item.total_amount = item.total_amount / CR

    context = {'forecast_data': forecast_data, 'active_nav': 'sales_forecast'}
    return render(request, 'planner/sales_forecast.html', context)

def project_list_view(request):
    form = ProjectForm()
    if request.method == 'POST':
        project_id = request.POST.get('project_id_hidden')
        
        # If a project ID is present, we are editing an existing project.
        if project_id:
            instance = get_object_or_404(Project, pk=project_id)
            form = ProjectForm(request.POST, instance=instance)
        # Otherwise, we are creating a new project.
        else:
            form = ProjectForm(request.POST)
            
        if form.is_valid():
            form.save()
            return redirect('project_list')
        # If form is not valid, the view will re-render with the form object
        # containing the errors, which you can display in your template.
    
    projects = Project.objects.select_related('segment').prefetch_related('activities').all()
    
    total_activities_count = Activity.objects.count()
    today = date.today()
    pending_activities_count = Activity.objects.filter(start_date__gt=today).count()
    active_projects_count = projects.filter(activities__isnull=False).distinct().count()
    
    context = {
        'form': form, 
        'projects': projects, 
        'active_nav': 'projects',
        'total_activities_count': total_activities_count,
        'pending_activities_count': pending_activities_count,
        'active_projects_count': active_projects_count,
    }
    return render(request, 'planner/project_list.html', context)


# MODIFIED: This view now serializes data for the frontend
def consolidated_planner_view(request):
    form = ActivityForm()
    grouping_method = request.GET.get('group_by', 'project')
    if request.method == 'POST' and 'add_activity' in request.POST:
        form = ActivityForm(request.POST)
        if form.is_valid():
            form.save()
            query_string = urlencode({'group_by': grouping_method})
            return redirect(f"{reverse('consolidated_planner')}?{query_string}")

    # Start with the base queryset
    all_activities_qs = Activity.objects.select_related('project', 'project_type__category', 'assignee').all()
    
    # Prepare the common context data using our simplified helper function
    context = _prepare_gantt_context(all_activities_qs)

    # Grouping logic remains specific to this view
    display_data = defaultdict(list)
    if grouping_method == 'engineer':
        # Use the processed list from the context
        sorted_activities = sorted(context['activities'], key=lambda a: (a.assignee.name if a.assignee else "Unassigned", a.start_date))
        for act in sorted_activities:
            display_data[act.assignee.name if act.assignee else "Unassigned"].append(act)
    else: 
        grouping_method = 'project'
        activities_by_project = defaultdict(list)
        # Use the processed list from the context
        for act in context['activities']:
            activities_by_project[act.project_id].append(act)
        for project in Project.objects.order_by('project_id'):
            display_data[project.project_id] = sorted(activities_by_project.get(project.id, []), key=lambda a: a.start_date)

    # Serialize only the data the frontend needs to render the bars
    gantt_init_data = {
        'activities': [
            {
                'pk': act.pk,
                'name': act.activity_name,
                'assignee': act.assignee.name if act.assignee else None,
                'start_date': act.start_date.isoformat() if act.start_date else None,
                'end_date': act.end_date.isoformat() if act.end_date else None,
            } for act in context['activities'] # Use the original full list
        ],
        'holidays': [h.isoformat() for h in context['holidays_map'].keys()],
        'today': context['today'].isoformat()
    }

    # Update context with view-specific data
    context.update({
        'form': form,
        'active_nav': 'projects',
        'display_data': dict(display_data),
        'grouping_method': grouping_method,
        'gantt_init_data': gantt_init_data 
    })
    return render(request, 'planner/activity_planner.html', context)

# MODIFIED: This view also serializes data for the frontend
def activity_planner_view(request, project_pk):
    project = get_object_or_404(Project, pk=project_pk)
    form = ActivityForm(initial={'project': project})
    if request.method == 'POST' and 'add_activity' in request.POST:
        form = ActivityForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('activity_planner', project_pk=project.pk)

    # Get the activities for this specific project
    activities_qs = Activity.objects.filter(project=project).select_related(
        'project', 'project_type__category', 'assignee'
    ).order_by('start_date')

    # Use the same helper function to get all the Gantt data
    context = _prepare_gantt_context(activities_qs)
    
    gantt_init_data = {
        'activities': [
            {
                'pk': act.pk,
                'name': act.activity_name,
                'assignee': act.assignee.name if act.assignee else None,
                'start_date': act.start_date.isoformat() if act.start_date else None,
                'end_date': act.end_date.isoformat() if act.end_date else None,
            } for act in context['activities'] # context['activities'] is already filtered
        ],
        'holidays': [h.isoformat() for h in context['holidays_map'].keys()],
        'today': context['today'].isoformat()
    }

    # Update context with view-specific data
    context.update({
        'project': project,
        'form': form,
        'active_nav': 'projects',
        'gantt_init_data': gantt_init_data
    })
    return render(request, 'planner/activity_planner.html', context)

# NEW: Helper function to get common workforce context
def _get_workforce_context():
    return {
        'workforce_counts': {
            'engineers': Employee.objects.filter(designation='ENGINEER', is_active=True).count(),
            'team_leads': Employee.objects.filter(designation='TEAM_LEAD', is_active=True).count(),
            'managers': Employee.objects.filter(designation='MANAGER', is_active=True).count(),
        },
        'all_employees': Employee.objects.all(),
        'designation_choices': Employee.DESIGNATION_CHOICES,
        'active_nav': 'workforce',
    }

def workforce_view(request):
    error_message = None
    entered_data = {}
    
    if request.method == 'POST' and 'add_employee' in request.POST:
        name = request.POST.get('name')
        designation = request.POST.get('designation')
        is_active_val = request.POST.get('is_active')
        is_active = True if is_active_val == 'True' else False
        
        if name and designation:
            # Check for duplicates (case-insensitive)
            if Employee.objects.filter(name__iexact=name).exists():
                error_message = f"Team member with name '{name}' already exists."
                entered_data = {'name': name, 'designation': designation, 'is_active': is_active_val}
            else:
                Employee.objects.create(name=name, designation=designation, is_active=is_active)
                return redirect('workforce')
    
    # Use helper for context
    context = _get_workforce_context()
    context.update({
        'error_message': error_message,
        'entered_data': entered_data,
    })
    return render(request, 'planner/workforce.html', context)

# NEW: View to handle employee updates
def update_employee_view(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    if request.method == 'POST':
        name = request.POST.get('name')
        designation = request.POST.get('designation')
        is_active_val = request.POST.get('is_active')
        is_active = True if is_active_val == 'True' else False

        if name and designation:
            # Check duplicates excluding current employee
            if Employee.objects.filter(name__iexact=name).exclude(pk=pk).exists():
                # Re-render the workforce page with error
                context = _get_workforce_context()
                context['error_message'] = f"Cannot update: Team member with name '{name}' already exists."
                return render(request, 'planner/workforce.html', context)
            
            employee.name = name
            employee.designation = designation
            employee.is_active = is_active
            employee.save()
            return redirect('workforce')
            
    return redirect('workforce')

def toggle_employee_status_view(request, pk):
    if request.method == 'POST':
        employee = get_object_or_404(Employee, pk=pk)
        employee.is_active = not employee.is_active
        employee.save()
    return redirect('workforce')

def configuration_view(request):
    if request.method == 'POST':
        if 'add_holiday' in request.POST:
            Holiday.objects.get_or_create(date=request.POST.get('holiday_date'), defaults={'description': request.POST.get('description')})
        elif 'add_project_type' in request.POST:
            segment = get_object_or_404(Segment, pk=request.POST.get('segment'))
            category = get_object_or_404(Category, pk=request.POST.get('category'))
            ProjectType.objects.get_or_create(segment=segment, category=category, defaults={
                'engineer_involvement': request.POST.get('engineer_involvement'),
                'team_lead_involvement': request.POST.get('team_lead_involvement'),
                'manager_involvement': request.POST.get('manager_involvement')
            })
        elif 'update_general_settings' in request.POST:
            general_settings, _ = GeneralSettings.objects.get_or_create(pk=1)
            general_settings.working_hours_per_day = request.POST.get('working_hours_per_day', 8.0)
            general_settings.save()
        elif 'update_capacity_settings' in request.POST:
            for choice, _ in Employee.DESIGNATION_CHOICES:
                setting, _ = CapacitySettings.objects.get_or_create(designation=choice)
                setting.monthly_meeting_hours = request.POST.get(f'meeting_hours_{choice}', 0)
                setting.monthly_leave_hours = request.POST.get(f'leave_hours_{choice}', 0)
                setting.efficiency_loss_factor = request.POST.get(f'efficiency_{choice}', 0)
                setting.save()
        return redirect('configuration')

    context = {
        'general_settings': GeneralSettings.objects.get_or_create(pk=1)[0],
        'capacity_settings': {c: CapacitySettings.objects.get_or_create(designation=c)[0] for c, _ in Employee.DESIGNATION_CHOICES},
        'all_segments': Segment.objects.all(), 'all_categories': Category.objects.all(),
        'project_types': ProjectType.objects.select_related('segment', 'category').all(),
        'holidays': Holiday.objects.all().order_by('date'), 'designations': Employee.DESIGNATION_CHOICES,
        'active_nav': 'configuration',
    }
    return render(request, 'planner/configuration.html', context)

def delete_project_view(request, pk):
    get_object_or_404(Project, pk=pk).delete()
    return redirect('project_list')

def delete_employee_view(request, pk):
    get_object_or_404(Employee, pk=pk).delete()
    return redirect('workforce')

def delete_holiday_view(request, pk):
    get_object_or_404(Holiday, pk=pk).delete()
    return redirect('configuration')

def edit_activity_view(request, pk):
    activity = get_object_or_404(Activity, pk=pk)
    
    next_url = request.GET.get('next')
    default_redirect_url = reverse('activity_planner', kwargs={'project_pk': activity.project.pk})
    
    if request.method == 'POST':
        form = ActivityForm(request.POST, instance=activity)
        if form.is_valid():
            form.save()
            return redirect(next_url or default_redirect_url)
    else:
        form = ActivityForm(instance=activity)
        
    context = {
        'activity': activity, 
        'form': form, 
        'project': activity.project,
        'next_url': next_url or default_redirect_url
    }
    return render(request, 'planner/edit_activity.html', context)

def delete_activity_view(request, pk):
    activity = get_object_or_404(Activity, pk=pk)
    project_pk = activity.project.pk
    
    next_url = request.POST.get('next')
    
    activity.delete()
    
    default_redirect_url = reverse('activity_planner', kwargs={'project_pk': project_pk})
    return redirect(next_url or default_redirect_url)

def edit_project_type_view(request, pk):
    project_type = get_object_or_404(ProjectType, pk=pk)
    next_url = request.GET.get('next')
    default_redirect_url = reverse('configuration')
    
    if request.method == 'POST':
        project_type.segment = get_object_or_404(Segment, pk=request.POST.get('segment'))
        project_type.category = get_object_or_404(Category, pk=request.POST.get('category'))
        project_type.engineer_involvement = request.POST.get('engineer_involvement')
        project_type.team_lead_involvement = request.POST.get('team_lead_involvement')
        project_type.manager_involvement = request.POST.get('manager_involvement')
        project_type.save()
        
        next_url_from_post = request.POST.get('next')
        return redirect(next_url_from_post or default_redirect_url)
    
    context = {
        'type': project_type, 
        'all_segments': Segment.objects.all(), 
        'all_categories': Category.objects.all(),
        'next_url': next_url or default_redirect_url
    }
    return render(request, 'planner/edit_project_type.html', context)

def delete_project_type_view(request, pk):
    get_object_or_404(ProjectType, pk=pk).delete()
    return redirect('configuration')

def capacity_plan_view(request):
    today = date.today()
    months = [(today.replace(day=1) + timedelta(days=31*i)).replace(day=1) for i in range(12)]
    month_keys = [m.strftime('%Y-%m') for m in months]
    
    general_settings, _ = GeneralSettings.objects.get_or_create(pk=1)
    holidays = list(Holiday.objects.values_list('date', flat=True))
    capacity_settings = {c: CapacitySettings.objects.get_or_create(designation=c)[0] for c, _ in Employee.DESIGNATION_CHOICES}
    
    # MODIFIED: Calculate supply using only ACTIVE employees
    workforce_counts = {
        'ENGINEER': Employee.objects.filter(designation='ENGINEER', is_active=True).count(),
        'TEAM_LEAD': Employee.objects.filter(designation='TEAM_LEAD', is_active=True).count(),
        'MANAGER': Employee.objects.filter(designation='MANAGER', is_active=True).count()
    }
    
    supply_data = defaultdict(dict)
    for month in months:
        _, num_days_in_month = calendar.monthrange(month.year, month.month)
        working_days_in_month = count_working_days(date(month.year, month.month, 1), date(month.year, month.month, num_days_in_month), holidays)
        for designation, count in workforce_counts.items():
            settings = capacity_settings[designation]
            month_key = month.strftime('%Y-%m')
            gross_hours = count * working_days_in_month * general_settings.working_hours_per_day
            non_project_hours = count * (settings.monthly_meeting_hours + settings.monthly_leave_hours)
            efficiency_loss = (gross_hours - non_project_hours) * (settings.efficiency_loss_factor / 100)
            supply_data[designation][month_key] = {'available_hours': gross_hours - non_project_hours - efficiency_loss, 'headcount': count}

    demand_hours = defaultdict(lambda: defaultdict(float))
    # Note: Demand is based on ACTIVITIES. Even if assigned to inactive user, work remains.
    for activity in Activity.objects.select_related('assignee').filter(assignee__isnull=False, start_date__isnull=False, end_date__isnull=False):
        daily_hours = general_settings.working_hours_per_day
        current_date = activity.start_date
        while current_date <= activity.end_date:
            if current_date.weekday() < 5 and current_date not in holidays:
                demand_hours[activity.assignee.designation][current_date.strftime('%Y-%m')] += daily_hours
            current_date += timedelta(days=1)

    project_types_with_brackets = ProjectType.objects.prefetch_related('effort_brackets')
    pt_bracket_map = {pt.id: list(pt.effort_brackets.all()) for pt in project_types_with_brackets}
    pt_map = {(pt.segment.name, pt.category.name): pt.id for pt in ProjectType.objects.select_related('segment', 'category')}
    project_type_map = {pt.id: pt for pt in project_types_with_brackets}

    for forecast in SalesForecast.objects.filter(start_date__isnull=False, end_date__isnull=False):
        pt_id = pt_map.get((forecast.segment, forecast.category))
        if not pt_id: continue
        
        brackets = pt_bracket_map.get(pt_id, [])
        calculated_effort_days = calculate_effort_from_value(forecast.total_amount, brackets)
        if calculated_effort_days <= 0: continue
        
        total_window_days = count_working_days(forecast.start_date, forecast.end_date, holidays)
        if total_window_days <= 0: continue
        
        daily_effort_factor = calculated_effort_days / total_window_days
        p_type = project_type_map.get(pt_id)
        if not p_type: continue
        
        daily_eng_hours = general_settings.working_hours_per_day * (p_type.engineer_involvement / 100) * daily_effort_factor
        daily_tl_hours = general_settings.working_hours_per_day * (p_type.team_lead_involvement / 100) * daily_effort_factor
        daily_mgr_hours = general_settings.working_hours_per_day * (p_type.manager_involvement / 100) * daily_effort_factor
        current_date = forecast.start_date
        while current_date <= forecast.end_date:
            if current_date.weekday() < 5 and current_date not in holidays:
                month_key = current_date.strftime('%Y-%m')
                demand_hours['ENGINEER'][month_key] += daily_eng_hours
                demand_hours['TEAM_LEAD'][month_key] += daily_tl_hours
                demand_hours['MANAGER'][month_key] += daily_mgr_hours
            current_date += timedelta(days=1)
    
    live_workload_by_month = defaultdict(float)
    forecasted_workload_by_month = defaultdict(float)
    
    for activity in Activity.objects.select_related('assignee').filter(
        assignee__isnull=False, start_date__isnull=False, end_date__isnull=False
    ):
        current_date = activity.start_date
        while current_date <= activity.end_date:
            if current_date.weekday() < 5 and current_date not in holidays:
                month_key = current_date.strftime('%Y-%m')
                live_workload_by_month[month_key] += general_settings.working_hours_per_day
            current_date += timedelta(days=1)
    
    for forecast in SalesForecast.objects.filter(start_date__isnull=False, end_date__isnull=False):
        pt_id = pt_map.get((forecast.segment, forecast.category))
        if not pt_id:
            continue
        
        brackets = pt_bracket_map.get(pt_id, [])
        calculated_effort_days = calculate_effort_from_value(forecast.total_amount, brackets)
        if calculated_effort_days <= 0:
            continue
        
        total_window_days = count_working_days(forecast.start_date, forecast.end_date, holidays)
        if total_window_days <= 0:
            continue
        
        daily_effort_factor = calculated_effort_days / total_window_days
        p_type = project_type_map.get(pt_id)
        if not p_type:
            continue
        
        total_daily_hours = general_settings.working_hours_per_day * daily_effort_factor * (
            (p_type.engineer_involvement + p_type.team_lead_involvement + p_type.manager_involvement) / 100
        )
        
        current_date = forecast.start_date
        while current_date <= forecast.end_date:
            if current_date.weekday() < 5 and current_date not in holidays:
                month_key = current_date.strftime('%Y-%m')
                forecasted_workload_by_month[month_key] += total_daily_hours
            current_date += timedelta(days=1)
    
    chart_data = []
    for month in months:
        month_key = month.strftime('%Y-%m')
        month_label = month.strftime('%b %Y')
        
        live_hours = live_workload_by_month.get(month_key, 0)
        forecast_hours = forecasted_workload_by_month.get(month_key, 0)
        
        chart_data.append({
            'month': month_label,
            'live_workload': round(live_hours, 1),
            'forecasted_workload': round(forecast_hours, 1),
            'total': round(live_hours + forecast_hours, 1)
        })
    
    report = []
    for des_value, des_display in Employee.DESIGNATION_CHOICES:
        des_data = {'designation': des_display, 'months': []}
        for month_key in month_keys:
            supply = supply_data[des_value].get(month_key, {})
            available_hours = supply.get('available_hours', 0)
            headcount = supply.get('headcount', 0)
            required_hours = demand_hours[des_value].get(month_key, 0)
            hours_per_person = (available_hours / headcount) if headcount > 0 else 0
            required_headcount = (required_hours / hours_per_person) if hours_per_person > 0 else 0
            des_data['months'].append({
                'month': month_key, 
                'available_hours': available_hours, 
                'required_hours': required_hours, 
                'variance_hours': available_hours - required_hours, 
                'available_headcount': headcount, 
                'required_headcount': required_headcount
            })
        report.append(des_data)
    
    context = {
        'active_nav': 'capacity_plan', 
        'report_data': report,
        'chart_data': chart_data
    }
    return render(request, 'planner/capacity_plan.html', context)

def help_view(request):
    context = {'active_nav': 'help'}; return render(request, 'planner/help_page.html', context)

def get_effort_brackets_for_project_type(request, pk):
    project_type = get_object_or_404(ProjectType, pk=pk)
    brackets_data = []
    for bracket in project_type.effort_brackets.all():
        brackets_data.append({
            'id': bracket.id,
            'project_value': bracket.project_value / CR,
            'effort_days': bracket.effort_days
        })
    return JsonResponse({'brackets': brackets_data})

@require_POST
def add_effort_bracket_for_project_type(request, pk):
    project_type = get_object_or_404(ProjectType, pk=pk)
    data = json.loads(request.body)
    try:
        value_in_cr = float(data.get('project_value'))
        full_value = value_in_cr * CR
        
        bracket, created = EffortBracket.objects.update_or_create(
            project_type=project_type,
            project_value=full_value,
            defaults={'effort_days': int(data.get('effort_days'))}
        )
        response_data = {
            'status': 'success', 'id': bracket.id,
            'project_value': bracket.project_value / CR,
            'effort_days': bracket.effort_days, 'created': created
        }
        return JsonResponse(response_data)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@require_POST
def delete_effort_bracket_view(request, pk):
    get_object_or_404(EffortBracket, pk=pk).delete()
    return JsonResponse({'status': 'success'})