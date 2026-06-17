allowed_klasifikasi = ['Umum', 'Rahasia', 'Terbatas']
klas_str = ", ".join(f"'{k}'" for k in allowed_klasifikasi)
print(klas_str)
