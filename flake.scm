;; Typed flake outputs for hurdkit's Guix GNU/Hurd images.
;;
;; The Nix flake remains the host bootstrap: it pins the goonix command that
;; reads this file. This file is data apart from its outputs procedure.
(flake
 (format 1)
 (description "Hurdkit GNU/Hurd images, drivers and tests")
 (modules "guix/modules")
 (inputs
  (goonix (git "https://github.com/hurdkit/goonix")
          (branch "goonix")
          (as library)))
 (outputs
  (lambda (in)
    (use-modules (guix packages)
                 (guix profiles)
                 (gnu system)
                 (gnu system image)
                 ((gnu image) #:select (image-type-constructor))
                 ((gnu system images hurd) #:select (hurd64-qcow2-image-type)))
    (define (project-system name)
      (load (self-file in (string-append "guix/" name ".scm"))))
    (define project-packages
      (map (lambda (name)
             (cons name
                   (module-ref (resolve-interface `(hurdkit packages ,name))
                               (string->symbol
                                (case name
                                  ((hurd) "hurd-hurdkit")
                                  ((base) "hurd-epoll-shim")
                                  (else (error "unknown project package" name)))))))
           '(base hurd)))
    `((packages ,@project-packages)
      (systems
       (hurd64 . ,(project-system "hurd64"))
       (hurd64-hurdkit . ,(project-system "hurd64-hurdkit"))
       (hurd64-platform . ,(project-system "hurd64-platform"))
       (hurd64-wayland . ,(project-system "hurd64-wayland")))
      (images
       (hurd64 . ,((image-type-constructor hurd64-qcow2-image-type)
                    (project-system "hurd64"))))
      (shells
       (default . ,(shell (packages
                           (list (@ (gnu packages virtualization) qemu)
                                 (@ (gnu packages ssh) openssh))))))))))
