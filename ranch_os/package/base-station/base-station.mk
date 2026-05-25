################################################################################
# base-station
################################################################################

BASE_STATION_VERSION = 1.0.0
BASE_STATION_SITE = $(BR2_EXTERNAL_RANCH_OS_PATH)/package/base-station
BASE_STATION_SITE_METHOD = local

BASE_STATION_DEPENDENCIES = sudo

define BASE_STATION_USERS
	basesetup -1 basesetup -1 * - /sbin/nologin - Captive portal user
endef

define BASE_STATION_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/base_station.py \
		$(TARGET_DIR)/usr/bin/base_station.py
	$(INSTALL) -D -m 0755 $(@D)/provision.py \
		$(TARGET_DIR)/usr/bin/provision.py
	$(INSTALL) -D -m 0755 $(@D)/ranch-wifi-connect.sh \
		$(TARGET_DIR)/usr/bin/ranch-wifi-connect
	$(INSTALL) -D -m 0755 $(@D)/ranch-wifi-watchdog.py \
		$(TARGET_DIR)/usr/bin/ranch-wifi-watchdog
	$(INSTALL) -D -m 0440 $(@D)/basesetup.sudoers \
		$(TARGET_DIR)/etc/sudoers.d/basesetup
endef

define BASE_STATION_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(@D)/base-station.service \
		$(TARGET_DIR)/usr/lib/systemd/system/base-station.service
	$(INSTALL) -D -m 0644 $(@D)/base-provision.service \
		$(TARGET_DIR)/usr/lib/systemd/system/base-provision.service
	$(INSTALL) -D -m 0644 $(@D)/ranch-wifi-watchdog.service \
		$(TARGET_DIR)/usr/lib/systemd/system/ranch-wifi-watchdog.service
	mkdir -p $(TARGET_DIR)/etc/systemd/system/multi-user.target.wants
	ln -sf ../../../../usr/lib/systemd/system/base-station.service \
		$(TARGET_DIR)/etc/systemd/system/multi-user.target.wants/base-station.service
	ln -sf ../../../../usr/lib/systemd/system/base-provision.service \
		$(TARGET_DIR)/etc/systemd/system/multi-user.target.wants/base-provision.service
	# Note: ranch-wifi-watchdog.service and systemd-time-wait-sync.service
	# have no wants-symlinks here. Both are pulled in by base-station's
	# `Wants=` directive instead, because:
	#   - ranch-wifi-watchdog should only run while base-station runs
	#   - systemd-time-wait-sync wants-symlinks get wiped by `systemctl
	#     preset` at install time (probably because some preset disables
	#     it), so naming it directly in base-station.service's [Unit]
	#     section is the only reliable approach.
endef

$(eval $(generic-package))
