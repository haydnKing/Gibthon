from django.db import models
from django import forms
from Bio.SeqUtils.MeltingTemp import Tm_staluc
from Bio.Seq import reverse_complement, Seq
from django.conf import settings

from django.utils.safestring import mark_safe
from django.utils.encoding import force_unicode
from django.utils.html import conditional_escape

from Bio import SeqIO, Entrez
from Bio.SeqFeature import SeqFeature, FeatureLocation, ExactPosition
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Alphabet import IUPAC

import random


from annoying.fields import AutoOneToOneField


from fragment.models import Gene

import os, subprocess, shlex, sys, csv

def hybrid_options(t,settings):
	return ' -n DNA -t %.2f -T %.2f -N %.2f -M %.2f --mfold=5,-1,100 ' %(t + settings.ss_safety, t + settings.ss_safety, settings.na_salt, settings.mg_salt)

class Settings(models.Model):
	construct = AutoOneToOneField('Construct', related_name='settings')
	mg_salt = models.DecimalField(max_digits=3, decimal_places=2, default=0.0)
	na_salt = models.DecimalField(max_digits=3, decimal_places=2, default=1)
	ss_safety = models.PositiveSmallIntegerField(default=3)
	min_anneal_tm = models.PositiveSmallIntegerField(default=50)
	min_primer_tm = models.PositiveSmallIntegerField(default=60)
	min_overlap = models.PositiveSmallIntegerField(default=20)
	
	def __unicode__(self):
		return 'Settings for ' + self.construct.name

class SettingsForm(forms.ModelForm):
	class Meta:
		model = Settings
		exclude = ['construct']

class Warning(models.Model):
	primer = models.ForeignKey('Primer', related_name='warning')
	WARNING_TYPE = (
		('mp', 'MISPRIME'),
		('sp','SELF PRIME'),
		('ta','ANNEAL TM'),
		('tp','PRIMER TM'),
	)
	type = models.CharField(max_length=2, choices=WARNING_TYPE)
	text = models.CharField(max_length=150)

class Primer(models.Model):
	name = models.CharField(max_length=80)
	construct = models.ForeignKey('Construct', related_name='primer')
	flap = models.OneToOneField('PrimerHalf', related_name='flap')
	stick = models.OneToOneField('PrimerHalf', related_name='stick')
	boxplot = models.ImageField(upload_to='boxplots')
	
	class Meta:
		ordering = ['stick']
	
	def __unicode__(self):
		return self.name + ' (' + str(len(self.warning.all())) + ')'
		
	def csv(self):
		return [self.name, self.length(), self.tm(), self.seq()]
	
	def length(self):
		return len(self.seq())
		
	def seq(self):
		return self.flap.seq() + self.stick.seq()
	
	def seq_pretty(self):
		return str(self.flap.seq()).lower() + str(self.stick.seq()).upper()
		
	def tm(self):
		return round(Tm_staluc(str(self.seq())),2)
		
	def tm_len_anneal(self, target):
		while self.stick.tm() < target:
			if not self.stick.extend():
				w = Warning.objects.create(
					primer = self,
					type = 'tp',
					text = 'Anneal tm below target of ' + str(target) + '&deg;C ('+str(self.stick.tm())+'&deg;C)',
				)
				break
		self.stick.save()

	def tm_len_primer(self, target):
		while self.tm() < target:
			if not self.flap.extend():
				w = Warning.objects.create(
					primer = self,
					type = 'tp',
					text = 'Primer tm below target of ' + str(target) + '&deg;C ('+str(self.tm())+'&deg;C)',
				)
				break
		self.flap.save()
	
	def del_all(self):
		self.flap.delete()
		self.stick.delete()
		self.delete()
	
	def self_prime_check(self):
		name = str(self.name)
		cwd = os.getcwd()
		wd = '/home/bill/www/unafold/'
		os.chdir(wd)
		w = open(wd + name,'w')
		w.write(str(self.seq()))
		w.close()
		devnull = open(os.devnull, 'w')
		cline = 'hybrid-ss-min' + hybrid_options(self.tm(), self.construct.settings) + wd + name
		p = subprocess.check_call(shlex.split(cline), stdout=devnull, stderr=devnull)
		cline = 'boxplot_ng -t "Energy Dotplot for ' + name + ' " ' + wd + name + '.plot'
		p = subprocess.check_call(shlex.split(cline), stdout=devnull, stderr=devnull)
		ss = csv.DictReader(open(wd + name + '.plot','r'), delimiter='\t')
		warnings = []
		for r in ss:
			if int(r['j']) == len(self.seq()):
				warnings.append((r['length'],float(r['energy'])/10))
		cline = 'convert ' + wd + name + '.ps ' + wd + name + '.png'
		p = subprocess.check_call(shlex.split(cline), stdout=devnull, stderr=devnull)
		os.rename(wd+name+'.png',settings.MEDIA_ROOT+'unafold/'+name+'.png')
		for f in os.listdir('.'):
			if os.path.isfile(f) and f.startswith(name):
				os.remove(f)
		os.chdir(cwd)
		self.boxplot = name + '.png'
		self.warning.all().filter(type='sp').delete()
		self.save()
		for warning in warnings:
			w = Warning.objects.create(
				primer = self,
				type = 'sp',
				text = 'Potential self-priming of 3\' end! Length: ' + str(warning[0]) + ', dG: ' + str(warning[1]),
			)
			
			
	def corrprime(self):
		primer_name = str(self.name)
		fragment_name = str(self.construct.name) + '-' + str(self.stick.cfragment.fragment.name)
		cwd = os.getcwd()
		wd = '/home/bill/www/unafold/'
		os.chdir(wd)
		w = open(wd + primer_name,'w')
		w.write(str(self.seq()))
		w.close()
		w = open(wd + fragment_name,'w')
		w.write(str(reverse_complement(self.stick.seq())))
		w.close()
		devnull = open(os.devnull, 'w')
		cline = 'hybrid-min' + hybrid_options(self.tm(), self.construct.settings) + wd + fragment_name + ' ' + wd + primer_name
		p = subprocess.check_call(shlex.split(cline), stdout=devnull, stderr=devnull)
		
	
	def misprime_check(self):
		primer_name = str(self.name)
		fragment_name = str(self.construct.name) + '-' + str(self.stick.cfragment.fragment.name)
		cwd = os.getcwd()
		wd = '/home/bill/www/unafold/'
		os.chdir(wd)
		w = open(wd + primer_name,'w')
		w.write(str(self.seq()))
		w.close()
		w = open(wd + fragment_name,'w')
		if(self.stick.top):
			w.write(str(self.stick.cfragment.sequence()))
		else:
			w.write(str(reverse_complement(Seq(self.stick.cfragment.sequence()))))
		w.close()
		devnull = open(os.devnull, 'w')
		cline = 'hybrid-min' + hybrid_options(self.tm(), self.construct.settings) + wd + fragment_name + ' ' + wd + primer_name
		p = subprocess.check_call(shlex.split(cline), stdout=devnull, stderr=devnull)
		ss = csv.DictReader(open(wd + fragment_name + '-' + primer_name + '.plot'), delimiter='\t')
		warnings = []
		for r in ss:
			# length of annealing
			l = int(r['length'])
			# bp index from 3' end that is annealing
			j = len(self.seq()) - (int(r['j']) - len(self.stick.cfragment.sequence()))
			# bp index from 5' end of fragment
			i = (int(r['i']) + l - 1)
			if (j == 0 and i == len(self.stick.cfragment.sequence())) or (l == 1 or l == len(self.stick.seq())-1):
				# that's the priming we wanted! store the energy for comparison
				continue
			else:
				warnings.append((l,j,i,float(r['energy'])/10))
		self.warning.all().filter(type='mp').delete()
		for warning in warnings:
			w = Warning.objects.create(
				primer = self,
				type = 'mp',
				text= 'Potentital mis-priming ' + (str(warning[1]) + ' bp from ' if warning[1] > 0 else ' of ') + '3\' end of primer at bp ' + str(warning[2]) + ', length ' + str(warning[0]) + ', energy ' + str(warning[3]),
			)
		for f in os.listdir('.'):
			if os.path.isfile(f) and f.startswith(self.construct.name):
				os.remove(f)
		os.chdir(cwd)


class PrimerHalf(models.Model):
	cfragment = models.ForeignKey('ConstructFragment', related_name='ph')
	top = models.BooleanField()
	length = models.PositiveSmallIntegerField()
	
	def len(self):
		return len(str(self.seq()))
		
	def start(self):
		if self.top ^ self.isflap():
			return self.cfragment.end() - self.length
		else:
			return self.cfragment.start()
	
	def end(self):
		if self.top ^ self.isflap():
			return self.cfragment.end()
		else:
			return self.cfragment.start() + self.length
	
	def isflap(self):
		try: self.flap
		except: return False
		else: return True
		
	def extend(self):
		self.length += 1
		if self.start() < self.cfragment.start_feature.start or self.end() > self.cfragment.end_feature.end:
			self.length -= 1
			return False
		else:
			return True
	
	def seq_surround(self):
		s = Seq(self.cfragment.sequence())
		start = max(self.start()-20,0)
		end = min(self.end()+20,len(self.cfragment.sequence()))
		s = s[start:end]
		f = [self.cfragment.start()-1 <= i < self.cfragment.end() for i in range(start, end)]
		p = [self.start()-1 <= i < self.end() for i in range(start, end)]
		if self.top: 
			s = reverse_complement(s)
			f.reverse()
			p.reverse()
		bases = zip(s,f,p)
		s = ''
		for b in bases:
			s += '<td class="'+('feature ' if b[1] else '')+('primer' if b[2] else '') +'">'+b[0]+'</td>'
		return s
	
	def seq(self):
		s = Seq(self.cfragment.sequence())
		s = s[self.start()-1:self.end()]
		if self.top: s = reverse_complement(s)
		return s
		
	def tm(self):
		return round(Tm_staluc(str(self.seq())),2)
		
	def __unicode__(self):
		if self.isflap():
			return self.flap.name + ' (flap): ' + str(self.seq()) + ' (' + str(self.tm()) + ')'
		else:
			return self.stick.name + ' (stick): ' + str(self.seq()) + ' (' + str(self.tm()) + ')'

SHAPE_CHOICES = (
	('c', 'Circular'),
	('l', 'Linear'),
)
class Construct(models.Model):
	owner = models.ForeignKey('auth.User', null=True)
	name = models.CharField(max_length=80)
	description = models.CharField(max_length=2000)
	genbank = models.OneToOneField('fragment.Gene', blank=True, null=True, related_name='construct_master')
	fragments = models.ManyToManyField('fragment.Gene', through='ConstructFragment', blank=True, related_name='construct_slave')

	shape = models.CharField(max_length=1, choices=SHAPE_CHOICES)
	created = models.DateTimeField(auto_now_add=True)
	modified = models.DateTimeField(auto_now=True)

	def __unicode__(self):
		return self.name
		
	def sequence(self):
		dna = ''
		for f in self.cf.all():
			dna += f.sequence()
		return dna
	
	def features(self):
		acc = 0
		for fr in self.cf.all():
			fe = fr.features()
			if fr.direction == 'r':
				fe.reverse()
			for f in fe:
				if fr.direction == 'r':
					t  = f.start
					f.start = fr.fragment.length() - f.end + 1
					f.end = fr.fragment.length() - t + 1
				f.start -= fr.start() - acc - 1
				f.end -= fr.start() - acc - 1
				yield f
			acc += fr.end() - fr.start() + 1
	
	def features_pretty(self):
		acc = 0
		for fr in self.cf.all():
			fe = fr.features()
			if fr.direction == 'r':
				fe.reverse()
			for f in fe:
				if fr.direction == 'r':
					t  = f.start
					f.start = fr.fragment.length() - f.end + 1
					f.end = fr.fragment.length() - t + 1
				f.start -= fr.start() - acc - 1
				f.end -= fr.start() - acc - 1
				yield f.pretty() + (' [reverse]' if fr.direction == 'r' else '')
			acc += fr.end() - fr.start() + 1
	
	def gb(self):
		g = SeqRecord(
			Seq(self.sequence(),IUPAC.IUPACUnambiguousDNA()),
			id=self.name,
			name=self.name,
			description=self.description
		)
		g.features = [SeqFeature(FeatureLocation(ExactPosition(f.start-1),ExactPosition(f.end)), f.type, qualifiers=dict([[q.name,q.data] for q in f.qualifier.all()])) for f in self.features()]
		return g.format('genbank')
		
	def process(self):
		for p in self.primer.all():
			p.del_all()
		n = self.cf.count()
		for i,cf in enumerate(self.cf.all()):
			cfu = self.cf.all()[(i+1)%n]
			pt = Primer.objects.create(
				name = self.name + '-' + cf.fragment.name + '-top',
				construct = self,
				stick = PrimerHalf.objects.create(
					cfragment = cf,
					top = True,
					length = self.settings.min_overlap
				),
				flap = PrimerHalf.objects.create(
					cfragment = cfu,
					top = True,
					length = self.settings.min_overlap
				)
			)
			pt.self_prime_check()
			pt.misprime_check()
			yield ':%d'%((((2*i)+1)*45.0)/n)
			yield ' '*1024
			cfd = self.cf.all()[(i-1)%n]
			pb = Primer.objects.create(
				name = self.name + '-' + cf.fragment.name + '-bottom',
				construct = self,
				stick = PrimerHalf.objects.create(
					cfragment = cf,
					top = False,
					length = self.settings.min_overlap
				),
				flap = PrimerHalf.objects.create(
					cfragment = cfd,
					top = False,
					length = self.settings.min_overlap
				)
			)
			if self.settings.min_anneal_tm > 0:
				pt.tm_len_anneal(self.settings.min_anneal_tm)
				pb.tm_len_anneal(self.settings.min_anneal_tm)
			if self.settings.min_primer_tm > 0:
				pt.tm_len_primer(self.settings.min_primer_tm)
				pb.tm_len_primer(self.settings.min_primer_tm)
			pb.self_prime_check()
			pb.misprime_check()
			yield ':%d'%((((2*i)+2)*45.0)/n)
			yield ' '*1024
		yield ':100'


class BetterRadioFieldRenderer(forms.widgets.RadioFieldRenderer):

	def __init__(self, *args, **kwargs):
		super(BetterRadioFieldRenderer, self).__init__(*args, **kwargs)
	
	def __iter__(self):
		key = str(random.randint(1000,9999))
		for i, choice in enumerate(self.choices):
			yield BetterRadioInput(key, self.name, self.value, self.attrs.copy(), choice, i)

	def __getitem__(self, idx):
		choice = self.choices[idx] # Let the IndexError propogate
		return BetterRadioInput(self.name, self.value, self.attrs.copy(), choice, idx)
	
	def render(self):
		return mark_safe(u'\n'.join([u'%s' % force_unicode(w) for w in self]))

class BetterRadioInput(forms.widgets.RadioInput):
	def __init__(self, key, *args, **kwargs):
		super(BetterRadioInput, self).__init__(*args, **kwargs)
		self.name += key
		self.attrs['id'] += '_' + key
	def __unicode__(self):
		if 'id' in self.attrs:
			label_for = ' for="%s_%s"' % (self.attrs['id'], self.index)
		else:
			label_for = ''
		choice_label = conditional_escape(force_unicode(self.choice_label))
		return mark_safe(u'%s<label%s>%s</label>' % (self.tag(), label_for, choice_label))

class ConstructForm(forms.ModelForm):
	description = forms.CharField(widget=forms.Textarea)
	shape = forms.ChoiceField(
		widget=forms.RadioSelect(renderer = BetterRadioFieldRenderer),
		choices=SHAPE_CHOICES,
		initial='c'
	)
	class Meta:
		model = Construct
		exclude = ['genbank', 'fragments', 'settings', 'owner']
		

class ConstructFragment(models.Model):
	construct = models.ForeignKey('Construct', related_name='cf')
	fragment = models.ForeignKey('fragment.Gene', related_name='cf')
	order = models.PositiveIntegerField()
	DIRECTION_CHOICES = (
		('f', 'Forward'),
		('r', 'Reverse'),
	)
	direction = models.CharField(max_length=1, choices=DIRECTION_CHOICES)
	start_feature = models.ForeignKey('fragment.Feature', related_name='start_feature')
	start_offset = models.IntegerField()
	end_feature = models.ForeignKey('fragment.Feature', related_name='end_feature')
	end_offset = models.IntegerField()

	class Meta:
		ordering = ['order']
	
	def primer_top(self):
		for ph in self.ph.all():
			try: ph.stick
			except: continue
			else:
				if ph.top:
					return ph.stick
	
	def primer_bottom(self):
		for ph in self.ph.all():
			try: ph.stick
			except: continue
			else:
				if not ph.top:
					return ph.stick
	
	def features(self):
		feat = []
		for f in self.fragment.feature.all():
			if self.direction == 'f':
				if f.start >= self.start() and f.end <=self.end():
						feat.append(f)
			else:
				if (self.fragment.length() - f.end + 1) >= self.start() and (self.fragment.length() - f.start + 1) <=self.end():
					feat.append(f)
		return feat
	
	def start(self):
		if self.direction == 'f':
			return self.start_feature.start - self.start_offset
		else:
			return self.fragment.length() - self.start_feature.end - self.start_offset + 1
	
	def end(self):
		if self.direction == 'f':
			return self.end_feature.end - self.end_offset
		else:
			return self.fragment.length() - self.end_feature.start - self.end_offset + 1
	
	def sequence(self):
		seq = self.fragment.sequence
		if self.direction == 'r':
			seq = str(reverse_complement(Seq(seq)))
		return seq[self.start()-1:self.end()]
	
	def __unicode__(self):
		return self.construct.name + ' : ' + self.fragment.name + ' (' + str(self.order) + ')'
	
class FeatureListForm(forms.Form):
	DIRECTION_CHOICES = (
		('f', 'Forward'),
		('r', 'Reverse'),
	)
	start_feature = forms.ModelChoiceField('fragment.Feature', None, label='')
	finish_feature = forms.ModelChoiceField('fragment.Feature', None, label='')
	direction = forms.ChoiceField(widget=forms.RadioSelect(renderer = BetterRadioFieldRenderer), choices=DIRECTION_CHOICES)
	def __init__(self, _fragment, _construct, *args, **kwargs):
		sf = self.base_fields['start_feature']
		ff = self.base_fields['finish_feature']
		cf = _fragment.cf.get(fragment=_fragment.pk, construct=_construct.pk)
		sf.queryset = _fragment.feature.all()
		ff.queryset = _fragment.feature.all()
		sf.widget.choices = sf.choices
		ff.widget.choices = ff.choices
		sf.initial = cf.start_feature
		ff.initial = cf.end_feature
		print cf.direction == 'r'
		self.base_fields['direction'].initial = cf.direction
		super(FeatureListForm, self).__init__(*args, **kwargs)

def add_fragment(_construct, _fragment):
	o = len(_construct.fragments.all())
	cf = ConstructFragment(construct=_construct, fragment=_fragment, order = o, direction='f', start_feature=_fragment.feature.all()[0], end_feature=_fragment.feature.all()[0], start_offset=0, end_offset=0)
	cf.save()
	
