import json
import datetime

from django.conf import settings
from django.shortcuts import get_object_or_404, render
from django.forms import ModelForm
from django.http import HttpResponseRedirect
from django.core.urlresolvers import reverse_lazy, reverse
from django.utils.translation import ugettext_lazy as _
from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.utils.translation import get_language
from djangocms_blog.models import Post
from django.contrib import messages
from django.http import JsonResponse
from django.views.generic import View, DetailView, ListView

from .models import Supporter
from utils.forms import ContactUsForm
from django.views.generic.edit import FormView
from membership.calendar.calendar import BookCalendar
from membership.models import Calendar as CalendarModel, StripeCustomer


from utils.views import LoginViewMixin, SignupViewMixin, \
    PasswordResetViewMixin, PasswordResetConfirmViewMixin
from utils.forms import PasswordResetRequestForm
from utils.stripe_utils import StripeUtils


from .forms import LoginForm, SignupForm, MembershipBillingForm, BookingDateForm,\
    BookingBillingForm

from .models import MembershipType, Membership, MembershipOrder, Booking, BookingPrice,\
    BookingOrder

from .mixins import MembershipRequiredMixin, IsNotMemberMixin


class IndexView(TemplateView):
    template_name = "digitalglarus/old_index.html"


class LoginView(LoginViewMixin):
    template_name = "digitalglarus/login.html"
    form_class = LoginForm
    success_url = reverse_lazy('digitalglarus:membership_pricing')


class SignupView(SignupViewMixin):
    template_name = "digitalglarus/signup.html"
    form_class = SignupForm
    success_url = reverse_lazy('digitalglarus:login')


class PasswordResetView(PasswordResetViewMixin):
    template_name = 'digitalglarus/reset_password.html'
    success_url = reverse_lazy('digitalglarus:login')
    form_class = PasswordResetRequestForm
    template_email_path = 'digitalglarus/emails/'


class PasswordResetConfirmView(PasswordResetConfirmViewMixin):
    template_name = 'digitalglarus/confirm_reset_password.html'
    success_url = reverse_lazy('digitalglarus:login')


class HistoryView(TemplateView):
    template_name = "digitalglarus/history.html"

    def get_context_data(self, *args, **kwargs):
        context = super(HistoryView, self).get_context_data(**kwargs)
        supporters = Supporter.objects.all()
        context.update({
            'supporters': supporters
        })
        return context


class BookingSelectDatesView(LoginRequiredMixin, MembershipRequiredMixin, FormView):
    template_name = "digitalglarus/booking.html"
    form_class = BookingDateForm
    membership_redirect_url = reverse_lazy('digitalglarus:membership_pricing')
    login_url = reverse_lazy('digitalglarus:login')
    success_url = reverse_lazy('digitalglarus:booking_payment')

    def form_valid(self, form):
        user = self.request.user
        start_date = form.cleaned_data.get('start_date')
        end_date = form.cleaned_data.get('end_date')
        booking_days = (end_date - start_date).days + 1
        original_price, discount_price, free_days = Booking.\
            booking_price(user, start_date, end_date)
        self.request.session.update({
            'original_price': original_price,
            'discount_price': discount_price,
            'booking_days': booking_days,
            'free_days': free_days,
            'start_date': start_date.strftime('%m/%d/%Y'),
            'end_date': end_date.strftime('%m/%d/%Y'),
        })
        return super(BookingSelectDatesView, self).form_valid(form)


class BookingPaymentView(LoginRequiredMixin, MembershipRequiredMixin, FormView):
    template_name = "digitalglarus/booking_payment.html"
    form_class = BookingBillingForm
    membership_redirect_url = reverse_lazy('digitalglarus:membership_pricing')
    # success_url = reverse_lazy('digitalglarus:booking_payment')
    booking_needed_fields = ['original_price', 'discount_price', 'booking_days', 'free_days',
                             'start_date', 'end_date']

    def dispatch(self, request, *args, **kwargs):
        from_booking = all(field in request.session.keys()
                           for field in self.booking_needed_fields)
        if not from_booking:
            return HttpResponseRedirect(reverse('digitalglarus:booking'))

        return super(BookingPaymentView, self).dispatch(request, *args, **kwargs)

    def get_success_url(self, order_id):
        return reverse('digitalglarus:booking_orders_detail', kwargs={'pk': order_id})

    def get_form_kwargs(self):
        form_kwargs = super(BookingPaymentView, self).get_form_kwargs()
        form_kwargs.update({
            'initial': {
                'start_date': self.request.session.get('start_date'),
                'end_date': self.request.session.get('end_date'),
                'price': self.request.session.get('discount_price'),
            }
        })
        return form_kwargs

    def get_context_data(self, *args, **kwargs):
        context = super(BookingPaymentView, self).get_context_data(*args, **kwargs)

        booking_data = {key: self.request.session.get(key)
                        for key in self.booking_needed_fields}
        booking_price_per_day = BookingPrice.objects.get().price_per_day
        total_discount = booking_price_per_day * booking_data.get('free_days')
        booking_data.update({
            'booking_price_per_day': booking_price_per_day,
            'total_discount': total_discount,
            'stripe_key': settings.STRIPE_API_PUBLIC_KEY
        })
        context.update(booking_data)
        return context

    def form_valid(self, form):
        data = form.cleaned_data
        context = self.get_context_data()
        token = data.get('token')
        start_date = data.get('start_date')
        end_date = data.get('end_date')

        normal_price, final_price, free_days = Booking.\
            booking_price(self.request.user, start_date, end_date)

        # Get or create stripe customer
        customer = StripeCustomer.get_or_create(email=self.request.user.email,
                                                token=token)
        if not customer:
            form.add_error("__all__", "Invalid credit card")
            return self.render_to_response(self.get_context_data(form=form))

        # Make stripe charge to a customer
        stripe_utils = StripeUtils()
        charge_response = stripe_utils.make_charge(amount=final_price,
                                                   customer=customer.stripe_id)
        charge = charge_response.get('response_object')

        # Check if the payment was approved
        if not charge:
            context.update({
                'paymentError': charge_response.get('error'),
                'form': form
            })
            return render(self.request, self.template_name, context)

        charge = charge_response.get('response_object')

        # Create Billing Address
        billing_address = form.save()

        # Create membership plan
        booking_data = {
            'start_date': start_date,
            'end_date': end_date,
            'start_date': start_date,
            'free_days': free_days,
            'price': normal_price,
            'final_price': final_price,
        }
        booking = Booking.create(booking_data)

        # Create membership order
        order_data = {
            'booking': booking,
            'customer': customer,
            'billing_address': billing_address,
            'stripe_charge': charge,
            'amount': final_price,
            'original_price': normal_price,
            'special_month_price': BookingPrice.objects.last().special_month_price
        }
        order = BookingOrder.create(order_data)

        # request.session.update({
        #     'membership_price': membership.type.first_month_price,
        #     'membership_dates': membership.type.first_month_formated_range
        # })
        return HttpResponseRedirect(self.get_success_url(order.id))


class MembershipPricingView(TemplateView):
    template_name = "digitalglarus/membership_pricing.html"

    def get_context_data(self, **kwargs):
        context = super(MembershipPricingView, self).get_context_data(**kwargs)
        membership_type = MembershipType.objects.last()
        context.update({
            'membership_type': membership_type
        })
        return context


class MembershipPaymentView(LoginRequiredMixin, IsNotMemberMixin, FormView):
    template_name = "digitalglarus/membership_payment.html"
    login_url = reverse_lazy('digitalglarus:signup')
    form_class = MembershipBillingForm
    already_member_redirect_url = reverse_lazy('digitalglarus:membership_orders_list')

    def get_form_kwargs(self):
        self.membership_type = MembershipType.objects.get(name='standard')
        form_kwargs = super(MembershipPaymentView, self).get_form_kwargs()
        form_kwargs.update({
            'initial': {
                'membership_type': self.membership_type.id
            }
        })
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super(MembershipPaymentView, self).get_context_data(**kwargs)
        context.update({
            'stripe_key': settings.STRIPE_API_PUBLIC_KEY,
            'membership_type': self.membership_type
        })
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()

        if form.is_valid():
            data = form.cleaned_data
            context = self.get_context_data()
            token = data.get('token')
            membership_type = data.get('membership_type')

            # Get or create stripe customer
            customer = StripeCustomer.get_or_create(email=self.request.user.email,
                                                    token=token)
            if not customer:
                form.add_error("__all__", "Invalid credit card")
                return self.render_to_response(self.get_context_data(form=form))

            # Make stripe charge to a customer
            stripe_utils = StripeUtils()
            charge_response = stripe_utils.make_charge(amount=membership_type.first_month_price,
                                                       customer=customer.stripe_id)
            charge = charge_response.get('response_object')

            # Check if the payment was approved
            if not charge:
                context.update({
                    'paymentError': charge_response.get('error'),
                    'form': form
                })
                return render(request, self.template_name, context)

            charge = charge_response.get('response_object')

            # Create Billing Address
            billing_address = form.save()

            # Create membership plan
            membership_data = {'type': membership_type}
            membership = Membership.create(membership_data)

            # Create membership order
            order_data = {
                'membership': membership,
                'customer': customer,
                'billing_address': billing_address,
                'stripe_charge': charge,
                'amount': membership_type.first_month_price
            }
            MembershipOrder.create(order_data)

            request.session.update({
                'membership_price': membership.type.first_month_price,
                'membership_dates': membership.type.first_month_formated_range
            })
            return HttpResponseRedirect(reverse('digitalglarus:membership_activated'))

        else:
            return self.form_invalid(form)


class MembershipActivatedView(TemplateView):
    template_name = "digitalglarus/membership_activated.html"

    def get_context_data(self, **kwargs):
        context = super(MembershipActivatedView, self).get_context_data(**kwargs)
        membership_price = self.request.session.get('membership_price')
        membership_dates = self.request.session.get('membership_dates')
        context.update({
            'membership_price': membership_price,
            'membership_dates': membership_dates,
        })
        return context


class MembershipOrdersListView(LoginRequiredMixin, ListView):
    template_name = "digitalglarus/membership_orders_list.html"
    context_object_name = "orders"
    login_url = reverse_lazy('digitalglarus:login')
    model = MembershipOrder
    paginate_by = 10

    def get_context_data(self, **kwargs):
        context = super(MembershipOrdersListView, self).get_context_data(**kwargs)
        start_date, end_date = MembershipOrder.current_membership(self.request.user)
        context.update({
            'membership_start_date': start_date,
            'membership_end_date': end_date,
        })
        return context

    def get_queryset(self):
        queryset = super(MembershipOrdersListView, self).get_queryset()
        queryset = queryset.filter(customer__user=self.request.user)
        return queryset


class OrdersMembershipDetailView(LoginRequiredMixin, DetailView):
    template_name = "digitalglarus/membership_orders_detail.html"
    context_object_name = "order"
    login_url = reverse_lazy('digitalglarus:login')
    # permission_required = ['view_hostingorder']
    model = MembershipOrder

    def get_context_data(self, **kwargs):
        context = super(OrdersMembershipDetailView, self).get_context_data(**kwargs)
        start_date, end_date = self.object.get_membership_range_date()
        context.update({
            'membership_start_date': start_date,
            'membership_end_date': end_date,
        })
        return context


class OrdersBookingDetailView(LoginRequiredMixin, DetailView):
    template_name = "digitalglarus/booking_orders_detail.html"
    context_object_name = "order"
    login_url = reverse_lazy('digitalglarus:login')
    # permission_required = ['view_hostingorder']
    model = BookingOrder

    def get_context_data(self, *args, **kwargs):

        context = super(OrdersBookingDetailView, self).get_context_data(**kwargs)

        bookig_order = self.object
        booking = bookig_order.booking

        start_date = booking.start_date
        end_date = booking.end_date
        free_days = booking.free_days

        booking_days = (end_date - start_date).days + 1
        original_price = booking.price
        final_price = booking.final_price
        context.update({
            'original_price': original_price,
            'total_discount': original_price - final_price,
            'final_price': final_price,
            'booking_days': booking_days,
            'free_days': free_days,
            'start_date': start_date.strftime('%m/%d/%Y'),
            'end_date': end_date.strftime('%m/%d/%Y'),
        })

        return context


class BookingOrdersListView(LoginRequiredMixin, ListView):
    template_name = "digitalglarus/booking_orders_list.html"
    context_object_name = "orders"
    login_url = reverse_lazy('digitalglarus:login')
    model = BookingOrder
    paginate_by = 10

    def get_queryset(self):
        queryset = super(BookingOrdersListView, self).get_queryset()
        queryset = queryset.filter(customer__user=self.request.user)
        return queryset


############## OLD VIEWS 
class CalendarApi(View):
    def get(self,request,month,year):
        calendar = BookCalendar(request.user,requested_month=month).formatmonth(int(year),int(month))
        ret = {'calendar':calendar,'month':month,'year':year}
        return JsonResponse(ret)

    def post(self,request):
        pd = json.loads(request.POST.get('data',''))
        ret = {'status':'success'}
        CalendarModel.add_dates(pd,request.user)
        return JsonResponse(ret)

class ContactView(FormView):
    template_name = 'contact.html'
    form_class = ContactUsForm
    success_url = reverse_lazy('digitalglarus:contact')
    success_message = _('Message Successfully Sent')

    def form_valid(self, form):
        form.save()
        form.send_email()
        messages.add_message(self.request, messages.SUCCESS, self.success_message)
        return super(ContactView, self).form_valid(form)



class AboutView(TemplateView):
    template_name = "digitalglarus/about.html"

def detail(request, message_id):
    p = get_object_or_404(Message, pk=message_id)

    context = { 'message': p, }
    return render(request, 'digitalglarus/detail.html', context)

def about(request):
    return render(request, 'digitalglarus/about.html')

def home(request):
    return render(request, 'index.html')

def letscowork(request):
    return render(request, 'digitalglarus/letscowork.html')


def blog(request):
    tags = ["digitalglarus"]
    posts = Post.objects.filter(tags__name__in=tags, publish=True).translated(get_language())
    # posts = Post.objects.filter_by_language(get_language()).filter(tags__name__in=tags, publish=True)
    context = {
        'post_list': posts,
    }
    return render(request, 'glarus_blog/post_list.html', context)


def blog_detail(request, slug):
    # post = Post.objects.filter_by_language(get_language()).filter(slug=slug).first()

    post = Post.objects.translated(get_language(), slug=slug).first()
    context = {
        'post': post,
    }
    return render(request, 'glarus_blog/post_detail.html', context)


def support(request):
    return render(request, 'support.html')


def supporters(request):
    context = {
        'supporters': Supporter.objects.order_by('name')
    }
    return render(request, 'supporters.html', context)



