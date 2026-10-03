# Build with packaging/build-rpm.sh, which runs this in a clean Fedora container.

Name:           audiorouter
Version:        1.2.1
Release:        1%{?dist}
Summary:        Send each app's sound to its own channel, with its own effects
License:        GPL-3.0-or-later
URL:            https://github.com/lmck-dev/AudioRouter
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  gcc
# The noise suppression plugin links against librnnoise.so.0; no headers needed.
BuildRequires:  rnnoise
BuildRequires:  systemd-rpm-macros
BuildRequires:  desktop-file-utils
# %%check runs the whole suite, window tests included.
BuildRequires:  python3-pyqt6

Requires:       python3-pyqt6
Requires:       pipewire
Requires:       wireplumber
# pw-dump, pw-cli, pw-metadata
Requires:       pipewire-utils
# pactl and parec (moving streams, level meters)
Requires:       pipewire-pulseaudio
Requires:       pulseaudio-utils
# Without the LV2 loader no plugin effect can run.
Requires:       pipewire-module-filter-chain-lv2
# The built-in compressor and limiter
Requires:       lsp-plugins-lv2

# The effects toolbox. Installed by default, removable without breaking the app.
# The echo-cancel notice's Fix button (session.py); the window has its own.
Recommends:     libnotify
Recommends:     lv2-calf-plugins
Recommends:     lv2-zam-plugins
Recommends:     lv2-mdala-plugins
Recommends:     lv2-rubberband-plugins
Recommends:     lv2-swh-plugins
Recommends:     lv2-x42-plugins
Recommends:     lv2-guitarix-plugins
Recommends:     lv2-abGate
Recommends:     lv2-eq10q

%description
Audio Router gives every application's sound its own channel - Speakers,
Headphones, a game, a voice chat - and every channel its own effect chain.
Channels are native PipeWire filter-chains, so each one shows up in the
desktop's ordinary sound settings. Microphones can have channels too, and any
channel can be recorded after its effects.

%prep
%autosetup

%build
%pyproject_wheel
gcc %{optflags} -shared -fPIC -fvisibility=hidden %{build_ldflags} \
    -o audiorouter_rnnoise.so audiorouter/native/rnnoise/audiorouter_rnnoise.c \
    -l:librnnoise.so.0 -lm
gcc %{optflags} -shared -fPIC -fvisibility=hidden %{build_ldflags} \
    -o audiorouter_meter.so audiorouter/native/meter/audiorouter_meter.c -lm

%install
%pyproject_install
%pyproject_save_files audiorouter
bundle=%{buildroot}%{_libdir}/lv2/audiorouter-rnnoise.lv2
install -Dpm755 audiorouter_rnnoise.so "$bundle/audiorouter_rnnoise.so"
install -pm644 audiorouter/native/rnnoise/*.ttl "$bundle/"
bundle=%{buildroot}%{_libdir}/lv2/audiorouter-meter.lv2
install -Dpm755 audiorouter_meter.so "$bundle/audiorouter_meter.so"
install -pm644 audiorouter/native/meter/*.ttl "$bundle/"
install -Dpm644 packaging/audiorouter.service %{buildroot}%{_userunitdir}/audiorouter.service
desktop-file-install --dir=%{buildroot}%{_datadir}/applications packaging/audiorouter.desktop

%check
QT_QPA_PLATFORM=offscreen HOME="$PWD/.check-home" %{python3} -m unittest discover -s tests -t .

# Routing from login is switched on per user, by the window's first start
# (install.set_up_for_user): a package cannot enable a service in each session.
%post
%systemd_user_post audiorouter.service

%preun
%systemd_user_preun audiorouter.service

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/audiorouter
%{_bindir}/audiorouter-gui
%{_libdir}/lv2/audiorouter-rnnoise.lv2/
%{_libdir}/lv2/audiorouter-meter.lv2/
%{_userunitdir}/audiorouter.service
%{_datadir}/applications/audiorouter.desktop

%changelog
* Sat Oct 03 2026 lmck-dev <lmck.dev@gmail.com> - 1.2.1-1
- 1.2.1: relicensed under GPL-3.0-or-later; LICENSE file shipped

* Fri Oct 02 2026 lmck-dev <lmck.dev@gmail.com> - 1.2.0-1
- 1.2.0: plugin folders; LADSPA plugins as effects

* Fri Oct 02 2026 lmck-dev <lmck.dev@gmail.com> - 1.1.0-1
- 1.1.0: recording apps (Audacity, OBS) choose a channel in Playing now;
  Playing now can be dragged taller

* Fri Oct 02 2026 lmck-dev <lmck.dev@gmail.com> - 1.0.1-1
- 1.0.1: new mic channels start with echo cancellation on

* Fri Oct 02 2026 lmck-dev <lmck.dev@gmail.com> - 1.0.0-1
- 1.0.0: Bypass and the unified Channels tab, owner-tested

* Fri Oct 02 2026 lmck-dev <lmck.dev@gmail.com> - 0.9.7-1
- Channels tab: one list in mixer order; every channel reads IN / LEVEL / OUT

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.9.6-1
- Bypass: one switch stops every channel and routing, and brings it all back

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.9.5-1
- A Ko-fi link in the About window, to support Audio Router

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.9.4-1
- An About window: version, credits, licence and system details, with Copy details

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.9.3-1
- The user guide is set like a page: a reading column, more space, styled headings and tables

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.9.2-1
- A User Guide, opened from the window's new User Guide button

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.9.1-1
- Mixer strips line up row for row, whether they take a mic or apps

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.9.0-1
- Mic channels can mix in apps: each has a hidden companion that offers the mix as the mic
- The mixer reads Mics | Channels | Groups | Outputs, with the real devices' own volumes

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.8.1-1
- The window reconnects after a PipeWire restart instead of showing no devices
- "Default output" is no longer reported as a channel with no output device

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.8.0-1
- Notice when a lone WirePlumber restart breaks echo cancellation, with a Fix button

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.7.2-1
- Input strips on the mixer meter the mic without anything recording it

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.7.1-1
- The fader in the Channels view too, in step with the mixer

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.7.0-1
- Groups: an output channel can play into another, which masters their sum

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.6.1-1
- Click INSERTS on any mixer strip to fold the inserts away

* Thu Oct 01 2026 lmck-dev <lmck.dev@gmail.com> - 0.6.0-1
- Pan and solo on every mixer strip

* Wed Sep 30 2026 lmck-dev <lmck.dev@gmail.com> - 0.5.0-1
- A post-effects fader on every channel; the desktop volume becomes the strip's trim
- Studio-style meters: green, amber and red segments
- Easier-to-read inserts in the mixer

* Wed Sep 30 2026 lmck-dev <lmck.dev@gmail.com> - 0.4.0-1
- A mixer view, opened by default: every channel as a console strip

* Wed Sep 30 2026 lmck-dev <lmck.dev@gmail.com> - 0.3.0-1
- Levels follow the highlighted effect: what it receives and what it puts out
- Separate lists for outputs and inputs; Playing now and Remembered apps fold away

* Fri Sep 18 2026 lmck-dev <lmck.dev@gmail.com> - 0.2.0-1
- Echo cancellation per input channel: keep what the speakers play out of the mic

* Tue Sep 15 2026 lmck-dev <lmck.dev@gmail.com> - 0.1.0-1
- First package
