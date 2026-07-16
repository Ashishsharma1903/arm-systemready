LICENSE = "CLOSED"

S = "${WORKDIR}"

DEPENDS = "python3-native python3-dtschema-native "

SRC_URI = "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.1.3.tar.xz"
SRC_URI[sha256sum] = "be41c068e88f5242a19bccdbffbe077b18c47b45f627e2325504b4fab79dd1dc"

do_install(){
    install -d ${D}${bindir}/linux-7.1.3/bindings
    cp -r ${S}/linux-7.1.3/Documentation/devicetree/bindings ${D}/${bindir}/linux-7.1.3/bindings
    dt-mk-schema -j ${D}/${bindir}/linux-7.1.3/bindings > processed_schema.json
    cp -r ${S}/processed_schema.json ${D}/${bindir}/
}

FILES:${PN} += "${bindir}/*"
