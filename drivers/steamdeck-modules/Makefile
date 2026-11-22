obj-m += steamdeck.o
obj-m += steamdeck-hwmon.o
obj-m += leds-steamdeck.o

KDIR ?= /lib/modules/$(shell uname -r)/build

modules:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

modules_install:
	$(MAKE) -C $(KDIR) M=$(CURDIR) INSTALL_MOD_DIR=extra modules_install

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
