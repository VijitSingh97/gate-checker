################################################################################
# gate-client
################################################################################

GATE_CLIENT_VERSION = 1.0.0
GATE_CLIENT_SITE = $(BR2_EXTERNAL_RANCH_OS_PATH)/package/gate-client
GATE_CLIENT_SITE_METHOD = local

define GATE_CLIENT_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/gate_client.py \
		$(TARGET_DIR)/usr/bin/gate_client.py
	$(INSTALL) -D -m 0755 $(@D)/ranch-gate-config-migrate.sh \
		$(TARGET_DIR)/usr/bin/ranch-gate-config-migrate
endef

define GATE_CLIENT_INSTALL_INIT_SYSTEMD
	$(INSTALL) -D -m 0644 $(@D)/gate-client.service \
		$(TARGET_DIR)/usr/lib/systemd/system/gate-client.service
	$(INSTALL) -D -m 0644 $(@D)/gate-config-migrate.service \
		$(TARGET_DIR)/usr/lib/systemd/system/gate-config-migrate.service
	mkdir -p $(TARGET_DIR)/etc/systemd/system/multi-user.target.wants
	ln -sf ../../../../usr/lib/systemd/system/gate-client.service \
		$(TARGET_DIR)/etc/systemd/system/multi-user.target.wants/gate-client.service
	ln -sf ../../../../usr/lib/systemd/system/gate-config-migrate.service \
		$(TARGET_DIR)/etc/systemd/system/multi-user.target.wants/gate-config-migrate.service
endef

$(eval $(generic-package))
